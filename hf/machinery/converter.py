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

import os, sys, re, traceback, logging
from models.symbol import *
from models.routine import Routine, AnalyzableRoutine
from models.module import Module, ModuleStub
from models.region import RegionType, RoutineSpecificationRegion
from tools.metadata import *
from tools.commons import UsageError, BracketAnalyzer, stacktrace
from tools.analysis import SymbolDependencyAnalyzer, getAnalysisForSymbol, getArguments
from machinery.parser import H90CallGraphAndSymbolDeclarationsParser, getSymbolsByName, currFile, currLineNo
from machinery.commons import conversionOptions, parseSpecification

def getSymbolsByModuleNameAndSymbolName(cgDoc, moduleNodesByName, symbolAnalysisByRoutineNameAndSymbolName={}):
    symbolsByModuleNameAndSymbolName = {}
    for moduleName in moduleNodesByName.keys():
        moduleNode = moduleNodesByName.get(moduleName)
        if not moduleNode:
            continue
        symbolsByModuleNameAndSymbolName[moduleName] = getSymbolsByName(
            cgDoc,
            moduleNode,
            isModuleSymbols=True,
            symbolAnalysisByRoutineNameAndSymbolName=symbolAnalysisByRoutineNameAndSymbolName
        )
        for symbolName in symbolsByModuleNameAndSymbolName[moduleName]:
            symbol = symbolsByModuleNameAndSymbolName[moduleName][symbolName]
            symbol.sourceModule = moduleName
    return symbolsByModuleNameAndSymbolName

def getSymbolsByRoutineNameAndSymbolName(cgDoc, routineNodesByProcName, parallelRegionTemplatesByProcName, symbolAnalysisByRoutineNameAndSymbolName={}):
    symbolsByRoutineNameAndSymbolName = {}
    for procName in routineNodesByProcName:
        routine = routineNodesByProcName[procName]
        procName = routine.getAttribute('name')
        symbolsByRoutineNameAndSymbolName[procName] = getSymbolsByName(
            cgDoc,
            routine,
            parallelRegionTemplatesByProcName.get(procName,[]),
            isModuleSymbols=False,
            symbolAnalysisByRoutineNameAndSymbolName=symbolAnalysisByRoutineNameAndSymbolName
        )
    return symbolsByRoutineNameAndSymbolName

def addGlobalParallelDomainNames(symbols, globalParallelDomainNames):
    for symbol in symbols:
        symbol.globalParallelDomainNames = globalParallelDomainNames

class H90toF90Converter(H90CallGraphAndSymbolDeclarationsParser):
    currentLineNeedsPurge = False
    tab_insideSub = "\t\t"
    tab_outsideSub = "\t"

    def __init__(
        self,
        cgDoc,
        implementationsByTemplateName,
        moduleNodesByName=None,
        parallelRegionData=None,
        symbolAnalysisByRoutineNameAndSymbolName=None,
        symbolsByModuleNameAndSymbolName=None,
        symbolsByRoutineNameAndSymbolName=None,
        globalParallelDomainNames={}
    ):
        super(H90toF90Converter, self).__init__(
            cgDoc,
            moduleNodesByName=moduleNodesByName,
            parallelRegionData=parallelRegionData,
            implementationsByTemplateName=implementationsByTemplateName
        )
        self.globalParallelDomainNames = globalParallelDomainNames
        self.currSubroutineImplementationNeedsToBeCommented = False
        self.currParallelIterators = []
        self.currRoutine = None
        self.currRegion = None
        self.currParallelRegion = None
        self.currModule = None
        self.currCallee = None
        self.currParallelRegionRelationNode = None
        self.currParallelRegionTemplateNode = None
        self.prepareLineCalledForCurrentLine = False
        self.preparedBy = None
        self.modulesInFile = []
        self.prefix = ""
        self.appendixByModuleName = {}
        self.lastModuleName = None
        try:
            if symbolAnalysisByRoutineNameAndSymbolName != None:
                self.symbolAnalysisByRoutineNameAndSymbolName = symbolAnalysisByRoutineNameAndSymbolName
            else:
                symbolAnalyzer = SymbolDependencyAnalyzer(self.cgDoc)
                self.symbolAnalysisByRoutineNameAndSymbolName = symbolAnalyzer.getSymbolAnalysisByRoutine()
            if symbolsByModuleNameAndSymbolName != None:
                self.symbolsByModuleNameAndSymbolName = symbolsByModuleNameAndSymbolName
            else:
                self.symbolsByModuleNameAndSymbolName = getSymbolsByModuleNameAndSymbolName(self.cgDoc, self.moduleNodesByName, self.symbolAnalysisByRoutineNameAndSymbolName)
            addGlobalParallelDomainNames(
                sum([index.values() for index in self.symbolsByModuleNameAndSymbolName.values()], []),
                globalParallelDomainNames
            )

            if symbolsByRoutineNameAndSymbolName != None:
                self.symbolsByRoutineNameAndSymbolName = symbolsByRoutineNameAndSymbolName
            else:
                self.symbolsByRoutineNameAndSymbolName = getSymbolsByRoutineNameAndSymbolName(
                    self.cgDoc,
                    self.routineNodesByProcName,
                    self.parallelRegionTemplatesByProcName,
                    self.symbolAnalysisByRoutineNameAndSymbolName
                )
            addGlobalParallelDomainNames(
                sum([index.values() for index in self.symbolsByRoutineNameAndSymbolName.values()], []),
                globalParallelDomainNames
            )

        except UsageError as e:
            logging.error('%s' %(str(e)), extra={"hfLineNo":currLineNo, "hfFile":currFile})
            sys.exit(1)
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            logging.critical('Error when initializing h90 conversion: %s' %(str(e)), extra={"hfLineNo":currLineNo, "hfFile":currFile})
            logging.info(traceback.format_exc())
            sys.exit(1)

    def switchToNewRegion(self, regionClassName="Region", oldRegion=None):
        logging.debug("switching to new %s on line %i; called from:\n%s" %(
            regionClassName,
            self.lineNo,
            stacktrace()
        ))
        self.currRegion = self.currRoutine.createRegion(regionClassName, oldRegion)

    def endRegion(self):
        logging.debug("ending region on line %i" %(self.lineNo))
        self.currRegion = None

    def filterOutSymbolsAlreadyAliveInCurrentScope(self, symbolList):
        return [
            symbol for symbol in symbolList
            if not symbol.analysis \
            or ( \
                symbol.name not in self.symbolsByRoutineNameAndSymbolName.get(self.currRoutine.name, {}) \
                and symbol.analysis.argumentIndexByRoutineName.get(self.currRoutine.name, -1) == -1 \
            )
        ]

    def loadLine(self, line):
        if line == "":
            return
        if self.currRegion:
            self.currRegion.loadLine(line, self.symbolsOnCurrentLine + self.importsOnCurrentLine)
        elif self.currRoutine:
            self.currRoutine.loadLine(line, self.symbolsOnCurrentLine + self.importsOnCurrentLine)
        elif self.currModule:
            self.currModule.loadLine(line)
        elif not self.lastModuleName:
            self.prefix += line
        else:
            appendix = self.appendixByModuleName.get(self.lastModuleName, "")
            appendix += line
            self.appendixByModuleName[self.lastModuleName] = appendix

    #TODO: remove tab argument everywhere
    def prepareLine(self, line, tab):
        if self.prepareLineCalledForCurrentLine:
            raise Exception(
                "Line has already been prepared by %s - there is an error in the transpiler logic. Please contact the Hybrid Fortran maintainers. Parser state: %s; before branch: %s" %(
                    self.preparedBy,
                    self.state,
                    self.stateBeforeBranch
                )
            )
        self.preparedBy = ""
        if conversionOptions.debugPrint:
            import inspect
            self.preparedBy = inspect.getouterframes(inspect.currentframe(), 2)[1][3]
        self.prepareLineCalledForCurrentLine = True
        self.loadLine(line)

    def processModuleSymbolImportAndGetAdjustedLine(self, line, symbols):
        if len(symbols) == 0:
            return line
        return self.implementation.getImportSpecification(
            symbols,
            RegionType.MODULE_DECLARATION,
            parallelRegionPosition=None,
            parallelRegionTemplates=[],
        )

    def processCallMatch(self, subProcCallMatch):
        super(H90toF90Converter, self).processCallMatch(subProcCallMatch)
        sourceModuleByNameInScope = {}
        for (sourceModule, nameInScope) in self.currModuleImportsDict.keys() + self.currRoutineImportsDict.keys():
            sourceModuleByNameInScope[nameInScope] = sourceModule
        sourceModuleName = sourceModuleByNameInScope.get(self.currCalleeName)
        sourceModule = ModuleStub(sourceModuleName) if sourceModuleName else self.currModule
        calleeNode = self.routineNodesByProcName.get(self.currCalleeName)
        if calleeNode:
            callerNode = self.routineNodesByProcName[self.currSubprocName]
            callerImplementation = self.implementationForTemplateName(
                callerNode.getAttribute('implementationTemplate')
            )
            calleeImplementation = self.implementationForTemplateName(calleeNode.getAttribute('implementationTemplate'))
            self.currCallee = AnalyzableRoutine(
                self.currCalleeName,
                sourceModule.name,
                calleeNode,
                self.parallelRegionTemplatesByProcName.get(self.currCalleeName),
                calleeImplementation,
                moduleRequiresStrongReference=isinstance(sourceModule, ModuleStub)
            )
        else:
            self.currCallee = Routine(
                self.currCalleeName,
                sourceModule.name,
                moduleRequiresStrongReference=isinstance(sourceModule, ModuleStub)
            )
        self.currRoutine.loadCall(self.currCallee)
        remainingCall = subProcCallMatch.group(2)
        if isinstance(self.currCallee, AnalyzableRoutine):
            self.analyseSymbolInformationOnCurrentLine(subProcCallMatch.group(0), isInsideSubroutineCall=True)
        self.currCallee.loadArguments(self.currArguments)
        self.prepareLine("", self.tab_insideSub)
        if self.state != "inside_subroutine_call" and not (self.state == "inside_branch" and self.stateBeforeBranch == "inside_subroutine_call"):
            self.currCallee = None
            self.processCallPost()

    def processModuleDeclarationLineAndGetAdjustedLine(self, line):
        baseline = line
        if self.currentLineNeedsPurge:
            baseline = "" #$$$ this seems dangerous
        adjustedLine = self.processModuleSymbolImportAndGetAdjustedLine(baseline, self.importsOnCurrentLine)
        if len(self.symbolsOnCurrentLine) > 0:
            adjustedLine = self.implementation.adjustDeclarationForDevice(
                adjustedLine,
                self.symbolsOnCurrentLine,
                None,
                RegionType.MODULE_DECLARATION,
                self.currRoutine.node.getAttribute('parallelRegionPosition') if self.currRoutine else "inside"
            )
        return adjustedLine

    def processTemplateMatch(self, templateMatch):
        super(H90toF90Converter, self).processTemplateMatch(templateMatch)
        self.prepareLine("","")

    def processTemplateEndMatch(self, templateEndMatch):
        super(H90toF90Converter, self).processTemplateEndMatch(templateEndMatch)
        self.prepareLine("","")

    def processBranchMatch(self, branchMatch):
        super(H90toF90Converter, self).processBranchMatch(branchMatch)
        self.prepareLine("","")
        self.currentLineNeedsPurge = True

    def processModuleBeginMatch(self, moduleBeginMatch):
        super(H90toF90Converter, self).processModuleBeginMatch(moduleBeginMatch)
        self.implementation.processModuleBegin(self.currModuleName)
        self.currModule = Module(
            self.currModuleName,
            self.moduleNodesByName[self.currModuleName]
        )
        self.modulesInFile.append(self.currModule)
        self.prepareLine(moduleBeginMatch.group(0), self.tab_outsideSub)

    def processModuleEndMatch(self, moduleEndMatch):
        self.prepareLine(moduleEndMatch.group(0), self.tab_outsideSub)
        self.lastModuleName = self.currModule.name
        self.currModule = None
        self.implementation.processModuleEnd()
        super(H90toF90Converter, self).processModuleEndMatch(moduleEndMatch)

    def processProcBeginMatch(self, subProcBeginMatch):
        super(H90toF90Converter, self).processProcBeginMatch(subProcBeginMatch)
        self.currRoutine = self.currModule.createRoutine(
            self.currSubprocName,
            self.routineNodesByProcName.get(self.currSubprocName),
            self.parallelRegionTemplatesByProcName.get(self.currSubprocName),
            self.implementation
        )
        self.currRoutine.loadSymbolsByName(self.currSymbolsByName)
        self.currRoutine.loadArguments(self.currArguments)
        self.currRoutine.loadGlobalContext(
            self.moduleNodesByName,
            self.symbolAnalysisByRoutineNameAndSymbolName,
            self.symbolsByModuleNameAndSymbolName
        )
        self.currRegion = self.currRoutine.currRegion
        self.prepareLine("", self.tab_insideSub)

    def processProcExitPoint(self, line, isSubroutineEnd):
        if isSubroutineEnd:
            self.prepareLine("", self.tab_outsideSub)
        else:
            self.switchToNewRegion("RoutineEarlyExitRegion")
            self.prepareLine(line, self.tab_insideSub)
            self.switchToNewRegion()

    def processProcEndMatch(self, subProcEndMatch):
        self.currRoutine.loadAllImports(self.currRoutineImportsDict)
        self.endRegion()
        self.processProcExitPoint(subProcEndMatch.group(0), isSubroutineEnd=True)
        self.currSubroutineImplementationNeedsToBeCommented = False
        self.currRoutine.finalize()
        self.currRoutine = None
        super(H90toF90Converter, self).processProcEndMatch(subProcEndMatch)

    def processParallelRegionMatch(self, parallelRegionMatch):
        super(H90toF90Converter, self).processParallelRegionMatch(parallelRegionMatch)
        logging.debug(
            "...parallel region starts on line %i with active symbols %s" %(self.lineNo, str(self.currSymbolsByName.values())),
            extra={"hfLineNo":currLineNo, "hfFile":currFile}
        )
        templateRelations = self.parallelRegionTemplateRelationsByProcName.get(self.currRoutine.name)
        if templateRelations:
            for templateRelation in templateRelations:
                startLine = templateRelation.getAttribute("startLine")
                if startLine in [None, '']:
                    continue
                startLineInt = 0
                try:
                    startLineInt = int(startLine)
                except ValueError:
                    raise Exception("Invalid startLine definition for parallel region template relation: %s\n. All active template relations: %s\nRoutine node: %s" %(
                        templateRelation.toxml(),
                        [templateRelation.toxml() for templateRelation in templateRelations],
                        self.currRoutine.node.toprettyxml()
                    ))
                if startLineInt == self.lineNo:
                    self.currParallelRegionRelationNode = templateRelation
                    break
        templates = self.parallelRegionTemplatesByProcName.get(self.currRoutine.name)
        if self.currParallelRegionRelationNode and templates:
            activeTemplateID = self.currParallelRegionRelationNode.getAttribute("id")
            for template in templates:
                if template.getAttribute("id") == activeTemplateID:
                    self.currParallelRegionTemplateNode = template
                    break
            else:
                raise Exception("No parallel region template has matched the active template ID.")
        if self.currParallelRegionRelationNode:
            self.currParallelIterators = self.implementation.getIterators(self.currParallelRegionTemplateNode)
        self.switchToNewRegion("ParallelRegion")
        self.currParallelRegion = self.currRegion
        if self.currParallelRegionTemplateNode:
            self.currParallelRegion.loadActiveParallelRegionTemplate(self.currParallelRegionTemplateNode)
        self.prepareLine("", self.tab_insideSub)

    def processParallelRegionEndMatch(self, parallelRegionEndMatch):
        super(H90toF90Converter, self).processParallelRegionEndMatch(parallelRegionEndMatch)
        self.prepareLine("", self.tab_insideSub)
        self.switchToNewRegion(oldRegion=self.currParallelRegion)
        self.currParallelRegion = None
        self.currParallelIterators = []
        self.currParallelRegionTemplateNode = None
        self.currParallelRegionRelationNode = None

    def processDomainDependantMatch(self, domainDependantMatch):
        super(H90toF90Converter, self).processDomainDependantMatch(domainDependantMatch)
        self.prepareLine("", "")

    def processDomainDependantEndMatch(self, domainDependantEndMatch):
        super(H90toF90Converter, self).processDomainDependantEndMatch(domainDependantEndMatch)
        self.prepareLine("", "")

    def processContainsMatch(self, containsMatch):
        super(H90toF90Converter, self).processContainsMatch(containsMatch)
        self.prepareLine(containsMatch.group(0), self.tab_outsideSub)

    def processDataStatementMatch(self, dataStatementMatch):
        if not self.currRegion or not isinstance(self.currRegion, RoutineSpecificationRegion):
            raise Exception("invalid place for a data statement")
        self.currRegion.loadDataSpecificationLine(dataStatementMatch.group(0))
        self.prepareLine("", "")

    def processInterfaceMatch(self, interfaceMatch):
        super(H90toF90Converter, self).processInterfaceMatch(interfaceMatch)
        self.prepareLine(interfaceMatch.group(0), self.tab_outsideSub)

    def processInterfaceEndMatch(self, interfaceEndMatch):
        super(H90toF90Converter, self).processInterfaceEndMatch(interfaceEndMatch)
        self.prepareLine(interfaceEndMatch.group(0), self.tab_outsideSub)

    def processTypeMatch(self, typeMatch):
        super(H90toF90Converter, self).processTypeMatch(typeMatch)
        self.prepareLine(typeMatch.group(0), self.tab_outsideSub)

    def processTypeEndMatch(self, typeEndMatch):
        super(H90toF90Converter, self).processTypeEndMatch(typeEndMatch)
        self.prepareLine(typeEndMatch.group(0), self.tab_outsideSub)

    def processNoMatch(self, line):
        super(H90toF90Converter, self).processNoMatch(line)
        self.prepareLine(line, "")

    def processInsideModuleState(self, line):
        super(H90toF90Converter, self).processInsideModuleState(line)
        if self.state not in ['inside_module', 'inside_branch'] \
        or (self.state == 'inside_branch' and self.stateBeforeBranch != 'inside_module'):
            return
        specificationStatementMatch = self.patterns.specificationStatementPattern.match(line)
        adjustedLine = line
        if specificationStatementMatch:
            adjustedLine = self.implementation.adjustSpecificationForDevice(line, specificationStatementMatch.group(1))
        if not self.prepareLineCalledForCurrentLine:
            self.prepareLine(self.processModuleDeclarationLineAndGetAdjustedLine(adjustedLine), self.tab_outsideSub)

    def processInsideDeclarationsState(self, line):
        '''process everything that happens per h90 declaration line'''
        subProcCallMatch = self.patterns.subprocCallPattern.match(line)
        parallelRegionMatch = self.patterns.parallelRegionPattern.match(line)
        domainDependantMatch = self.patterns.domainDependantPattern.match(line)
        subProcEndMatch = self.patterns.subprocEndPattern.match(line)
        templateMatch = self.patterns.templatePattern.match(line)
        templateEndMatch = self.patterns.templateEndPattern.match(line)
        branchMatch = self.patterns.branchPattern.match(line)
        dataStatementMatch = self.patterns.dataStatementPattern.match(line)

        if dataStatementMatch:
            self.processDataStatementMatch(dataStatementMatch)
            return
        if branchMatch:
            self.processBranchMatch(branchMatch)
            return
        if subProcCallMatch:
            self.switchToNewRegion("CallRegion")
            self.processCallMatch(subProcCallMatch)
            self.switchToNewRegion()
            return
        if subProcEndMatch:
            self.processProcEndMatch(subProcEndMatch)
            if self.state == "inside_branch":
                self.stateBeforeBranch = 'inside_module_body'
            else:
                self.state = 'inside_module_body'
            return
        if parallelRegionMatch:
            self.processParallelRegionMatch(parallelRegionMatch)
            if self.currParallelIterators:
                if self.state == "inside_branch":
                    self.stateBeforeBranch = "inside_parallelRegion"
                else:
                    self.state = 'inside_parallelRegion'
            return
        if self.patterns.subprocBeginPattern.match(line):
            raise UsageError("subprocedure within subprocedure not allowed")
        if templateMatch:
            raise UsageError("template directives are only allowed outside of subroutines")
        if templateEndMatch:
            raise UsageError("template directives are only allowed outside of subroutines")

        if domainDependantMatch:
            if self.state == "inside_branch":
                self.stateBeforeBranch = 'inside_domainDependantRegion'
            else:
                self.state = 'inside_domainDependantRegion'
            self.switchToNewRegion()
            self.processDomainDependantMatch(domainDependantMatch)
            return

        importMatch1 = self.patterns.importPattern.match(line)
        importMatch2 = self.patterns.singleMappedImportPattern.match(line)
        importMatch3 = self.patterns.importAllPattern.match(line)
        specTuple = parseSpecification(line)
        specificationStatementMatch = self.patterns.specificationStatementPattern.match(line)
        if not ( \
            line.strip() == "" \
            or line.strip().startswith("#") \
            or importMatch1 \
            or importMatch2 \
            or importMatch3 \
            or specTuple[0] \
            or specificationStatementMatch \
        ):
            if self.state == "inside_branch":
                self.stateBeforeBranch = "inside_subroutine_body"
            else:
                self.state = "inside_subroutine_body"
            self.switchToNewRegion()
            self.processInsideSubroutineBodyState(line)
            return

        self.analyseSymbolInformationOnCurrentLine(line)
        #we are never calling super and every match that would have prepared a line, would already have been covered
        #with a return -> safe to call prepareLine here.
        self.prepareLine(line, self.tab_insideSub)

    def processInsideSubroutineBodyState(self, line):
        '''process everything that happens per h90 subroutine body line'''
        branchMatch = self.patterns.branchPattern.match(line)
        if branchMatch:
            self.processBranchMatch(branchMatch)
            return

        if self.patterns.branchEndPattern.match(line):
            self.prepareLine("","")
            return

        subProcCallMatch = self.patterns.subprocCallPattern.match(line)
        if subProcCallMatch:
            self.switchToNewRegion("CallRegion")
            self.processCallMatch(subProcCallMatch)
            self.switchToNewRegion()
            return

        subProcEndMatch = self.patterns.subprocEndPattern.match(line)
        if subProcEndMatch:
            self.processProcEndMatch(subProcEndMatch)
            if self.state == "inside_branch":
                self.stateBeforeBranch = "inside_module_body"
            else:
                self.state = 'inside_module_body'
            return

        if self.patterns.earlyReturnPattern.match(line):
            self.processProcExitPoint(line, isSubroutineEnd=False)
            return

        if self.currSubroutineImplementationNeedsToBeCommented:
            self.prepareLine("! " + line, "")
            return

        parallelRegionMatch = self.patterns.parallelRegionPattern.match(line)
        if parallelRegionMatch:
            self.processParallelRegionMatch(parallelRegionMatch)
            if self.currParallelIterators:
                if self.state == "inside_branch":
                    self.stateBeforeBranch = "inside_parallelRegion"
                else:
                    self.state = 'inside_parallelRegion'
            return

        parallelRegionEndMatch = self.patterns.parallelRegionEndPattern.match(line)
        if parallelRegionEndMatch:
            #note: this may occur when a parallel region is discarded because it doesn't apply
            #-> state stays within body and the region end line will trap here
            self.processParallelRegionEndMatch(parallelRegionEndMatch)
            return

        domainDependantMatch = self.patterns.domainDependantPattern.match(line)
        if (domainDependantMatch):
            if self.state == "inside_branch":
                self.stateBeforeBranch = "inside_domainDependantRegion"
            else:
                self.state = 'inside_domainDependantRegion'
            self.processDomainDependantMatch(domainDependantMatch)
            return

        if (self.patterns.subprocBeginPattern.match(line)):
            raise Exception("subprocedure within subprocedure not allowed")

        self.analyseSymbolInformationOnCurrentLine(line, isInSubroutineBody=True)
        self.prepareLine(line, self.tab_insideSub)

    def processInsideParallelRegionState(self, line):
        branchMatch = self.patterns.branchPattern.match(line)
        if branchMatch:
            self.processBranchMatch(branchMatch)
            return

        subProcCallMatch = self.patterns.subprocCallPattern.match(line)
        if subProcCallMatch:
            if subProcCallMatch.group(1) not in self.routineNodesByProcName.keys():
                message = self.implementation.warningOnUnrecognizedSubroutineCallInParallelRegion(
                    self.currRoutine.name,
                    subProcCallMatch.group(1)
                )
                if message != "":
                    logging.warning(message, extra={"hfLineNo":currLineNo, "hfFile":currFile})
            self.switchToNewRegion("CallRegion")
            self.processCallMatch(subProcCallMatch)
            self.switchToNewRegion()
            return

        parallelRegionEndMatch = self.patterns.parallelRegionEndPattern.match(line)
        if (parallelRegionEndMatch):
            self.processParallelRegionEndMatch(parallelRegionEndMatch)
            self.state = "inside_subroutine_body"
            if self.state == "inside_branch":
                self.stateBeforeBranch = "inside_subroutine_body"
            else:
                self.state = 'inside_subroutine_body'
            return

        if (self.patterns.parallelRegionPattern.match(line)):
            raise Exception("parallelRegion within parallelRegion not allowed")
        if (self.patterns.subprocEndPattern.match(line)):
            raise Exception("subprocedure end before @end parallelRegion")
        if (self.patterns.subprocBeginPattern.match(line)):
            raise Exception("subprocedure within subprocedure not allowed")

        adjustedLine = ""
        whileLoopMatch = self.patterns.whileLoopPattern.match(line)
        loopMatch = self.patterns.loopPattern.match(line)
        if whileLoopMatch == None and loopMatch != None:
            adjustedLine += self.implementation.loopPreparation().strip() + '\n'
        adjustedLine += line
        self.analyseSymbolInformationOnCurrentLine(line, isInSubroutineBody=True)
        self.prepareLine(adjustedLine, self.tab_insideSub)

    def processInsideDomainDependantRegionState(self, line):
        super(H90toF90Converter, self).processInsideDomainDependantRegionState(line)
        if self.state == "inside_domainDependantRegion":
            self.prepareLine("", "")

    def processInsideModuleDomainDependantRegionState(self, line):
        super(H90toF90Converter, self).processInsideModuleDomainDependantRegionState(line)
        if self.state == "inside_moduleDomainDependantRegion":
            self.prepareLine("", "")

    def processInsideBranch(self, line):
        super(H90toF90Converter, self).processInsideBranch(line)
        if self.state != "inside_branch":
            self.prepareLine("", "")

    def processInsideIgnore(self, line):
        super(H90toF90Converter, self).processInsideIgnore(line)
        self.prepareLine("", "")

    def processLine(self, line):
        self.currentLineNeedsPurge = False
        self.prepareLineCalledForCurrentLine = False
        super(H90toF90Converter, self).processLine(line)
        if not self.prepareLineCalledForCurrentLine:
            raise Exception(
                "Line has never been prepared - there is an error in the transpiler logic. Please contact the Hybrid Fortran maintainers. Parser state: %s; before branch: %s" %(
                    self.state,
                    self.stateBeforeBranch
                )
            )

    def processFile(self, fileName):
        super(H90toF90Converter, self).processFile(fileName)

    def prepareFileContent(self, fileName):
        self.processFile(fileName)
        return {
            "fileName": fileName,
            "prefix": self.implementation.filePreparation(fileName) + self.prefix,
            "modules": self.modulesInFile,
            "appendixByModuleName": self.appendixByModuleName
        }