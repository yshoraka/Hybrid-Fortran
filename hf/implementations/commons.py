#!/usr/bin/python
# -*- coding: UTF-8 -*-

# Copyright (C) 2016 Michel Müller, Tokyo Institute of Technology

# This file is part of Hybrid Fortran.

# Hybrid Fortran is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Hybrid Fortran is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public License
# along with Hybrid Fortran. If not, see <http://www.gnu.org/licenses/>.

from tools.GeneralHelper import UsageError
from tools.DomHelper import appliesTo, getDomainsWithParallelRegionTemplate, getReductionScalarsByOperator, getTemplate
import logging

def getReductionClause(parallelRegionTemplate):
	reductionScalarsByOperator = getReductionScalarsByOperator(parallelRegionTemplate)
	return ", ".join([
		"reduction(%s: %s)" %(operator, ", ".join(reductionScalarsByOperator[operator]))
		for operator in reductionScalarsByOperator.keys()
	])

def getDataDirectiveAndUpdateOnDeviceFlags(currRoutineNode, currParallelRegionTemplates, dependantSymbols, createDeclaration, routineIsKernelCaller, enterOrExit='enter'):
	presentDeclaration = "present" # if currRoutineNode.getAttribute("parallelRegionPosition") == 'inside' else "deviceptr"
	copyDeclaration = "copyin"
	if enterOrExit != 'enter':
		copyDeclaration = "copyout"
	result = ""
	dataDeclarations = ""
	if enterOrExit == 'enter':
		dataDeclarations += "!$acc enter data "
	else:
		dataDeclarations += "!$acc exit data "
	dataDeclarationsRequired = False
	commaRequired = False
	for index, symbol in enumerate(dependantSymbols):
		logging.debug(
			"analyzing symbol %s for data directive. Domains: %s, IsHostSymbol: %s, IsPresent: %s, IsToBeTransfered: %s, SourceModule: %s, Intent: %s\n" %(
				symbol.name,
				str(symbol.domains),
				symbol.isHostSymbol,
				symbol.isPresent,
				symbol.isToBeTransfered,
				str(symbol.sourceModule),
				str(symbol.intent)
			)
		)
		#Rules that lead to a symbol not being touched by directives
		symbol.isOnDevice = False
		if not symbol.domains or len(symbol.domains) == 0:
			continue
		if symbol.isHostSymbol:
			continue
		if currRoutineNode.getAttribute('parallelRegionPosition') == 'within'\
		and (symbol.intent in ["in", "inout", "out", "unspecified"] or not symbol.sourceModule in [None,""]):
			symbol.isOnDevice = True
			continue
		if currRoutineNode.getAttribute('parallelRegionPosition') != 'inside'\
		and not symbol.sourceModule in [None,""]:
			continue

		#Rules for kernel wrapper routines and symbols declared to be transfered
		newDataDeclarations = ""
		if symbol.isPresent:
			if symbol.intent in ["in", "out", "inout", "unspecified"] or not symbol.sourceModule in [None,""]:
				#all we can do is marking the symbol correctly - OpenACC enter data doesn't support present check sadly
				#please note: unspecified intent is a symbol that is a dummy variable with no intent specified.
				if enterOrExit == 'enter':
					symbol.isOnDevice = True
				else:
					symbol.isOnDevice = False
				continue
			else:
				#no intent, no source module specified --> local variable
				if enterOrExit == 'enter':
					newDataDeclarations += "%s(%s)" %(createDeclaration, symbol.name)
					symbol.isOnDevice = True
				else:
					newDataDeclarations += "delete(%s)" %(symbol.name)
					symbol.isOnDevice = False
		else:
			if not routineIsKernelCaller and currRoutineNode.getAttribute('parallelRegionPosition') != 'within' and not symbol.isToBeTransfered:
				continue
			if symbol.intent == "in":
				if enterOrExit == 'enter':
					newDataDeclarations += "copyin(%s)" %(symbol.name)
					symbol.isOnDevice = True
				else:
					newDataDeclarations += "delete(%s)" %(symbol.name)
					symbol.isOnDevice = False
			elif symbol.intent == "inout" or not symbol.sourceModule in [None,""]:
				newDataDeclarations += "%s(%s)" %(copyDeclaration, symbol.name)
				if enterOrExit == 'enter':
					symbol.isOnDevice = True
				else:
					symbol.isOnDevice = True
			elif symbol.intent == "out":
				#We need to be careful here: Because of branching around kernels it could easily happen that
				#copyout data is not being written inside the data region, thus overwriting the host data with garbage.
				newDataDeclarations += "%s(%s)" %(copyDeclaration, symbol.name)
				if enterOrExit == 'enter':
					symbol.isOnDevice = True
				else:
					symbol.isOnDevice = False
			elif enterOrExit == 'enter':
				newDataDeclarations += "%s(%s)" %(createDeclaration, symbol.name)
				symbol.isOnDevice = True
			else:
				newDataDeclarations += "delete(%s)" %(symbol.name)
				symbol.isOnDevice = False

		#Wrapping up enter data / exit data
		if commaRequired == True:
			newDataDeclarations = ", " + newDataDeclarations
		dataDeclarations += newDataDeclarations
		dataDeclarationsRequired = True
		commaRequired = True

	dataDeclarations += "\n"
	if dataDeclarationsRequired == True:
		result += dataDeclarations
	return result, dataDeclarationsRequired

def getLoopOverSymbolValues(symbol, loopName, innerLoopImplementationFunc):
	result = ""
	if len(symbol.domains) > 0:
		result += "hf_tracing_outer_%s: " %(loopName)
	for domainNum in range(len(symbol.domains),0,-1):
		result += "do hf_tracing_enum%i = lbound(hf_tracing_temp_%s,%i), ubound(hf_tracing_temp_%s,%i)\n" %(domainNum, symbol.name, domainNum, symbol.name, domainNum)
	result += innerLoopImplementationFunc(symbol)
	for domainNum in range(len(symbol.domains),0,-1):
		result += "end do"
		if domainNum != 1:
			result+= '\n'
	if len(symbol.domains) > 0:
		result += " hf_tracing_outer_%s\n" %(loopName)
	return result

def getTracingDeclarationStatements(currRoutineNode, dependantSymbols, patterns, useReorderingByAdditionalSymbolPrefixes={'hf_tracing_temp_':False}):
	tracing_symbols = []
	if len(dependantSymbols) == 0 or currRoutineNode.getAttribute('parallelRegionPosition') == 'outside':
		return "", tracing_symbols

	result = "integer(8), save :: hf_tracing_counter = 0\n"
	result += "integer(4) :: hf_error_printed_counter\n"
	result += "character(len=256) :: hf_tracing_current_path\n"
	max_num_of_domains_for_symbols = 0
	for symbol in dependantSymbols:
		if len(symbol.domains) == 0:
		# or 'allocatable' in symbol.declarationPrefix \
		# or symbol.intent not in ['in', 'inout', 'out'] \
		# or symbol.isOnDevice and currRoutineNode.getAttribute('parallelRegionPosition') == 'inside':
			continue
		if len(symbol.domains) > max_num_of_domains_for_symbols:
			max_num_of_domains_for_symbols = len(symbol.domains)
		for prefix in useReorderingByAdditionalSymbolPrefixes.keys():
			current_declaration_line = symbol.getDeclarationLineForAutomaticSymbol(
				purgeList=['intent', 'public', 'allocatable', 'target'],
				name_prefix=prefix,
				use_domain_reordering=useReorderingByAdditionalSymbolPrefixes[prefix],
				skip_on_missing_declaration=True
			)
			if current_declaration_line == "":
				break
			result += current_declaration_line + '\n'
		else:
			tracing_symbols.append(symbol)

	if max_num_of_domains_for_symbols > 0:
		result += "integer(4) :: %s\n" %(
			', '.join(
				["hf_tracing_enum%i" %(domainNum) for domainNum in range(1,max_num_of_domains_for_symbols+1)]
			)
		)
	return result, tracing_symbols

number_of_traces = 200
def getTracingStatements(currRoutineNode, currModuleName, tracingSymbols, traceHandlingFunc, increment_tracing_counter=True, loop_name_postfix=''):
	def innerTempArraySetterLoopFunc(symbol):
		return "hf_tracing_temp_%s = %s\n" %(
			symbol.accessRepresentation(
				parallelIterators=[],
				offsets=["hf_tracing_enum%i" %(domainNum) for domainNum in range(1,len(symbol.domains)+1)],
				parallelRegionNode=None,
				use_domain_reordering=False
			),
			symbol.accessRepresentation(
				parallelIterators=[],
				offsets=["hf_tracing_enum%i" %(domainNum) for domainNum in range(1,len(symbol.domains)+1)],
				parallelRegionNode=None,
				use_domain_reordering=True
			)
		)

	result = ''
	if len(tracingSymbols) > 0 and currRoutineNode.getAttribute('parallelRegionPosition') != 'outside':
		result += "if (hf_tracing_counter .lt. %i) then\n" %(number_of_traces)
		for symbol in tracingSymbols:
			if 'allocatable' in symbol.declarationPrefix:
				result += "if (allocated(%s)) then\n" %(symbol.name)
			result += "hf_tracing_temp_%s = 0\n" %(
				symbol.accessRepresentation(
					parallelIterators=[],
					offsets=[":" for _ in range(len(symbol.domains))],
					parallelRegionNode=None,
					use_domain_reordering=False
				)
			)
			if symbol.isOnDevice:
				result += "!$acc update host(%s) if(hf_symbols_are_device_present)\n" %(symbol.name)
			result += getLoopOverSymbolValues(symbol, "%s_temp_%s" %(symbol.name, loop_name_postfix), innerTempArraySetterLoopFunc)
			result += traceHandlingFunc(currRoutineNode, currModuleName, symbol)
			if 'allocatable' in symbol.declarationPrefix:
				result += "end if\n"
		result += "end if\n"
	if increment_tracing_counter:
		result += "hf_tracing_counter = hf_tracing_counter + 1\n"
	return result

def tracingFilename(currModuleName, currRoutineNode, symbol, begin_or_end):
	filename_postfix = "%s_" %(begin_or_end)
	if symbol.intent == "inout" and begin_or_end == "begin":
		filename_postfix = "in_"
	elif symbol.intent == "inout":
		filename_postfix = "out_"
	elif symbol.intent not in ["", None]:
		filename_postfix = "%s_" %(symbol.intent)
	return "%s_%s_%s_%s" %(
		currModuleName,
		currRoutineNode.getAttribute('name'),
		symbol.name,
		filename_postfix
	)

def getCompareToTraceFunc(abortSubroutineOnError=True, loop_name_postfix='', begin_or_end='begin'):
	def printSomeErrors(symbol):
		accessor = symbol.accessRepresentation(
			parallelIterators=[],
			offsets=["hf_tracing_enum%i" %(domainNum) for domainNum in range(1,len(symbol.domains)+1)],
			parallelRegionNode=None,
			use_domain_reordering=False
		)
		result = ""
		result += "if ( ( abs( hf_tracing_comparison_%s - hf_tracing_temp_%s ) / hf_tracing_comparison_%s ) .gt. 1E-9) then \n" %(accessor, accessor, accessor)
		result += "write(0,*) 'error at:'\n"
		for domainNum in range(1,len(symbol.domains)+1):
			result += "write(0,*) 'domain %i:', hf_tracing_enum%i\n" %(domainNum, domainNum)
		result += "write(0,*) 'expected', hf_tracing_comparison_%s, 'actual', hf_tracing_temp_%s\n" %(accessor, accessor)
		result += "hf_error_printed_counter = hf_error_printed_counter + 1\n"
		result += "if (hf_error_printed_counter >= 5) then\n"
		result += "exit hf_tracing_outer_%s_printindex_%s\n" %(symbol.name, loop_name_postfix)
		result += "end if\n"
		result += "end if\n"
		return result

	def compareToTrace(currRoutineNode, currModuleName, symbol):
		result = "call findNewFileHandle(hf_tracing_imt)\n"
		result += "write(hf_tracing_current_path, '(A,I3.3,A)') './datatrace/%s', hf_tracing_counter, '.dat'\n" %(
			tracingFilename(currModuleName, currRoutineNode, symbol, begin_or_end)
		)
		result += "open(hf_tracing_imt, file=trim(hf_tracing_current_path), form='unformatted', status='old', action='read', iostat=hf_tracing_ierr)\n"
		result += "if (hf_tracing_ierr .ne. 0) then\n"
		result += "write(0,*) 'symbol %s trace n/a'\n" %(symbol.name)
		result += "close(hf_tracing_imt)\n"
		if abortSubroutineOnError:
			result += "hf_tracing_counter = hf_tracing_counter + 1\n"
			result += "return\n"
		result += "else\n"
		result += "read(hf_tracing_imt) hf_tracing_comparison_%s\n" %(symbol.name)
		result += "hf_num_of_elements = " + ' * '.join([
			"(ubound(hf_tracing_comparison_%s,%i) - lbound(hf_tracing_comparison_%s,%i) + 1)" %(symbol.name, domainNum, symbol.name, domainNum)
			for domainNum in range(1,len(symbol.domains)+1)
		]) + '\n'
		result += "if (hf_num_of_elements <= 0) then\n"
		result += "write(0,*) '%s: symbol %s ok. (array has no elements)'\n" %(currRoutineNode.getAttribute('name'), symbol.name)
		result += "else\n"
		if 'real' in symbol.declarationPrefix:
			result += "hf_mean_ref = sqrt(sum(hf_tracing_comparison_%s**2) / hf_num_of_elements)\n" %(symbol.name)
			result += "hf_mean_gpu = sqrt(sum(hf_tracing_temp_%s**2) / hf_num_of_elements)\n" %(symbol.name)
			result += "if (abs(hf_mean_ref) .gt. 1E-20) then\n"
			result += "hf_tracing_error = sqrt(sum((hf_tracing_comparison_%s - hf_tracing_temp_%s)**2 ) / hf_num_of_elements ) / hf_mean_ref\n" %(
				symbol.name,
				symbol.name
			)
			result += "else\n"
			result += "hf_tracing_error = sqrt(sum((hf_tracing_comparison_%s - hf_tracing_temp_%s)**2 ) / hf_num_of_elements )\n" %(
				symbol.name,
				symbol.name
			)
			result += "end if\n"
			result += "if (hf_tracing_error .ne. hf_tracing_error .or. hf_tracing_error .gt. 1E-9) then\n" # a .ne. a tests a for NaN in Fortran (needs -Kieee compiler flag in pgf90)
			result += "\
write(0,*) 'In module %s, subroutine %s, checkpoint %s:', 'Real Array %s does not match the data found in ', trim(hf_tracing_current_path), \
' RMS Error:', hf_tracing_error, ' NumOfValues:', hf_num_of_elements, ' ReferenceMean: ', hf_mean_ref, ' GPUMean: ', hf_mean_gpu\n\
			" %(
				currModuleName,
				currRoutineNode.getAttribute('name'),
				loop_name_postfix,
				symbol.name
			)
			result += "hf_error_printed_counter = 0\n"
			result += getLoopOverSymbolValues(symbol, "%s_printindex_%s" %(symbol.name, loop_name_postfix), printSomeErrors)
		else:
			result += "if (any(hf_tracing_comparison_%s .ne. hf_tracing_temp_%s)) then\n" %(symbol.name, symbol.name)
			result += "write(0,*) 'In module %s, subroutine %s, checkpoint %s::', 'Array %s does not match the data found in ./datatrace.'\n" %(
				currModuleName,
				currRoutineNode.getAttribute('name'),
				loop_name_postfix,
				symbol.name
			)
		result += "write(0,*) 'GPU version shape:'\n"
		result += "write(0,*) shape(hf_tracing_temp_%s)\n" %(symbol.name)
		result += "write(0,*) 'Reference version shape:'\n"
		result += "write(0,*) shape(hf_tracing_comparison_%s)\n" %(symbol.name)
		result += "hf_tracing_error_found = .true.\n"
		result += "else\n"
		result += "write(0,*) 'symbol %s ok. nRMSE: ', hf_tracing_error\n" %(symbol.name)
		result += "end if\n"
		result += "end if\n"
		result += "end if\n"
		result += "close(hf_tracing_imt)\n"
		return result
	return compareToTrace

def getVectorSizePPNames(parallelRegionTemplate):
	template = getTemplate(parallelRegionTemplate)
	template_prefix = ''
	if template != '':
		template_prefix = '_' + template
	return ["CUDA_BLOCKSIZE_X" + template_prefix, "CUDA_BLOCKSIZE_Y" + template_prefix, "CUDA_BLOCKSIZE_Z" + template_prefix]

def getIteratorDeclaration(currRoutineNode, currParallelRegionTemplates, architectures):
	result = ""
	iteratorsByName = {}
	if not currParallelRegionTemplates or not currRoutineNode.getAttribute('parallelRegionPosition') == 'within':
		return result
	for template in currParallelRegionTemplates:
		if not appliesTo(architectures, template):
			continue
		iteratorsByName.update(dict(
			(iterator, None)
			for iterator in [domain.name for domain in getDomainsWithParallelRegionTemplate(template)]
		))
	iterators = iteratorsByName.keys()
	if len(iterators) == 0:
		return result
	result += "integer(4) :: "
	for index, iterator in enumerate(iterators):
		if index != 0:
			result = result + ", "
		result = result + iterator
	result += "\n"
	return result

def getCUDAErrorHandling(calleeRoutineNode, errorVariable="cuerror", stopImmediately=True):
	name = calleeRoutineNode.getAttribute('name')
	if not name:
		raise Exception("Routine node without name")
	stopLine = ""
	if stopImmediately:
		stopLine = "stop 1\n"
	return  "%s = cudaThreadSynchronize()\n" \
			"%s = cudaGetLastError()\n" \
			"if(%s .NE. cudaSuccess) then\n"\
				"\twrite(0, *) 'CUDA error in kernel %s:', cudaGetErrorString(%s)\n%s" \
			"end if\n" %(errorVariable, errorVariable, errorVariable, name, errorVariable, stopLine)

def getDebugOffsetString(domainTuple, previousOffsets):
	def getUpperBound(domainSizeSpec):
		boundaries = domainSizeSpec.split(':')
		if len(boundaries) == 1:
			return boundaries[0].strip()
		if len(boundaries) == 2:
			return boundaries[1].strip()
		raise UsageError("Unexpected domain size specification: %s" %(domainSizeSpec))

	#$$$ change this - it must be consistant with storage_order.F90
	userdefinedDomNames = ["x", "y", "z", "nz", "i", "j", "vertical", "verticalPlus", "KMAX_CONST", "KMP1_CONST", "ntlm", "ngm", "id_qa_e", "1", "2", "3", "4"]
	(dependantDomName, dependantDomSize) = domainTuple
	upperBound = getUpperBound(dependantDomSize)
	offset = ""
	if dependantDomName in userdefinedDomNames:
		offset = "DEBUG_OUT_%s" %(dependantDomName.strip())
	elif upperBound in userdefinedDomNames:
		offset = "DEBUG_OUT_%s" %(upperBound)
	else:
		offset = "DEBUG_OUT_x"
	if offset in previousOffsets:
		#if there are multiple dimensions with the same sizes, use the second specified macro
		# - else we'd be always showing the diagonal of quadratic matrices.
		offset += "_2"
	return offset

def getRuntimeDebugPrintStatements(symbolsByName, calleeRoutineNode, parallelRegionNode, useOpenACC=True):
	def wrap_in_acc_pp(string, symbol):
		accPP = symbol.accPP()[0]
		if accPP == "":
			return string
		return accPP + "(" + string + ")"

	result = ""
	if calleeRoutineNode.getAttribute('parallelRegionPosition') == 'outside':
		result += "#ifndef GPU\n"
		result += "if (hf_debug_print_iterator == 0) then\n"

	routineName = calleeRoutineNode.getAttribute('name')
	if not routineName:
		raise Exception("Callee routine name undefined.")
	result += "write(0,*) '*********** kernel %s finished *************** '\n" %(routineName)
	symbolsToPrint = symbolsByName.values()
	# symbolNames = sorted(symbolsByName.keys())
	# symbolsToPrint = [
	#     symbolsByName[symbolName] for symbolName in symbolNames
	#     if symbolsByName[symbolName].domains and len(symbolsByName[symbolName].domains) != 0
	# ]
	offsetsBySymbolName = {}
	for symbol in symbolsToPrint:
		offsets = []
		for domain in symbol.domains:
			offsets.append(getDebugOffsetString(domain, offsets))
		offsetsBySymbolName[symbol.name] = offsets
	symbolClauses = [
		symbol.accessRepresentation(
			[],
			["%s:%s" %(offset, offset) for offset in offsetsBySymbolName[symbol.name]],
			parallelRegionNode
		)
		for symbol in symbolsToPrint
		if len(symbol.domains) > 0
	]
	if useOpenACC:
		result += "#ifdef GPU\n"
		result += "!$acc update if(hf_symbols_are_device_present) host(%s)\n" %(", ".join(symbolClauses)) if len(symbolsToPrint) > 0 else ""
		result += "#endif\n"
	for symbol in symbolsToPrint:
		result = result + "hf_output_temp = %s\n" %(symbol.accessRepresentation([], offsetsBySymbolName[symbol.name], parallelRegionNode))
		#michel 2013-4-18: the Fortran-style memcopy as used right now in the above line creates a runtime error immediately
		#                  if we'd like to catch such errors ourselves, we need to use the cuda API memcopy calls - however we
		#                  then also need information about the symbol size, which isn't available in the current implementation
		#                  (we currently look at the typedef just as a string).
		# result = result + "cuErrorMemcopy = cudaGetLastError()\n" \
		#     "if(cuErrorMemcopy .NE. cudaSuccess) then\n"\
		#         "\twrite(0, *) 'CUDA error when attempting to copy value from %s:', cudaGetErrorString(cuErrorMemcopy)\n" \
		#         "stop 1\n" \
		#     "end if\n" %(symbol.name)
		joinedDomains = offsetsBySymbolName[symbol.name]
		if len(joinedDomains) > 0:
			domainsStr = "(',"
			formStr = "'(A,"
			for i in range(len(joinedDomains)):
				if i != 0:
					domainsStr = domainsStr + ",', ',"
					formStr = formStr + ",A,"
				domainsStr = domainsStr + str(joinedDomains[i])
				formStr = formStr + "I3"
			formStr = formStr + ",A,E19.11)'"
			domainsStr = domainsStr + ",'):"
		else:
			formStr = "'(A,E16.8)'"
			domainsStr = "scalar"
		result = result + "write(0,%s) '%s@%s', hf_output_temp\n" %(formStr, symbol.name, domainsStr)
	result = result + "write(0,*) '**********************************************'\n"
	result = result + "write(0,*) ''\n"
	if calleeRoutineNode.getAttribute('parallelRegionPosition') == 'outside':
		result += "end if\n"
		result += "#endif\n"
	return result