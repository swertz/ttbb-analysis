#!/usr/bin/env python

# Python imports
import os, sys, argparse, stat
from math import sqrt
from pdb import set_trace as bp # insert bp() to have a breakpoint anywhere
import json

import ROOT
ROOT.gROOT.SetBatch()
ROOT.PyConfig.IgnoreCommandLineOptions = True

import CombineHarvester.CombineTools.ch as ch

import definitions as defs
import utils

def main():

    parser = argparse.ArgumentParser(description='Create shape datacards ready for combine')

    parser.add_argument('-i', '--input', required=True, help='Input ROOT file containing templates')
    parser.add_argument('-d', '--data', action='store_true', help='Use real data in SIGNAL REGION (by default, will transfer QCD shape from the CR1) - for now Asimov datasets are always used.')
    parser.add_argument('--bbb', action='store_true', help='Add bin-by-bin uncertainties')
    parser.add_argument('--fit-mode', required=True, choices=['shape_CR1', 'abcd'], help='Fit mode')
    parser.add_argument('--qcd-systs', dest='QCD_systs', action='store_true', help='If mode is "shape_CR1", add uncertainty on the QCD shape from the VR/CR2 ratio')
    parser.add_argument('--equal-bins', dest='equal_bins', action='store_true', help='Modify templates to have equal-width bins numbered 1 through nBins, without changing the bin contents. Makes plotting easier if some bins are very fine.')
    parser.add_argument('--rate-systs', nargs='*', help='Input any JSON files with theory rate systematics in the four regions')
    parser.add_argument('--exp-rate', nargs='*', help='Input any JSON files with experimental rate systematics in the four regions')
    parser.add_argument('--sub-folder', help='Select sub-folder inside the input ROOT file')
    parser.add_argument('-o', '--output', required=True, help='Output directory')

    options = parser.parse_args()

    prepareShapesAndCards(options)


def prepareShapesAndCards(options):

    cb = ch.CombineHarvester()

    if options.fit_mode == 'shape_CR1':
        cats = [
            (1, 'SR'),
            (2, 'CR1')
        ]
        print('-- QCD estimation: fit bin-by-bin by assuming shape in CR1 and SR is the same --')

    elif options.fit_mode == 'abcd':
        cats = [
            (1, 'SR'),
            (2, 'CR1'),
            (3, 'VR'),
            (4, 'CR2'),
        ]
        print('-- QCD etimation: bin-by-bin ABCD using the four regions --')
    
    processed_shapes = os.path.join(options.output, 'processed_shapes.root')
    QCD_VR_ratios, est_QCD_yields, QCD_shape_CR1, QCD_shape_CR2 = utils.extractShapes(options.input, processed_shapes, defs.tt_bkg + defs.other_bkg, defs.sig_processes, options.data, equal_bins=options.equal_bins, sub_folder=options.sub_folder)
    Nbins = len(QCD_VR_ratios)

    cb.AddObservations(['*'], ['ttbb'], ['13TeV_2016'], ['FH'], cats)
    
    cb.AddProcesses(['*'], ['ttbb'], ['13TeV_2016'], ['FH'], defs.sig_processes, cats, True)

    cb.AddProcesses(['*'], ['ttbb'], ['13TeV_2016'], ['FH'], defs.tt_bkg + defs.other_bkg, cats, False)

    ### QCD estimate: add all "delta" templates
    QCD_processes = [ 'QCD_bin_{}'.format(i+1) for i in range(Nbins) ]
    cb.AddProcesses(['*'], ['ttbb'], ['13TeV_2016'], ['FH'], QCD_processes, cats, False)


    ### Systematics
    added_theory_systs = []
    added_exp_systs = []

    # Modeling systematics, not on QCD! ###
    cbWithoutQCD = cb.cp().process_rgx(['QCD.*'], False)
    
    # Theory rate uncertainties from the JSON file
    if options.rate_systs is not None:
        for json_file in options.rate_systs:
            added_theory_systs += addRateSystematics(cb, json_file, options.sub_folder)
    
    # Experimental rate uncertainties from the JSON file
    if options.exp_rate is not None:
        for json_file in options.exp_rate:
            added_exp_systs += addRateSystematics(cb, json_file, options.sub_folder)

    # Luminosity
    cbWithoutQCD.AddSyst(cb, 'lumi_$ERA', 'lnN', ch.SystMap('era')(['13TeV_2016'], defs.getLumiUncertainty('13TeV_2016')))
    added_exp_systs.append('lumi_13TeV_2016')

    # Experimental systematics, common for all processes and categories
    for s in defs.exp_systs:
        # If we have added it already as a rate systematics, skip it!
        if s not in added_exp_systs:
            added_exp_systs.append(s)
            cbWithoutQCD.AddSyst(cb, s, 'shape', ch.SystMap()(1.))

    # Theory shape systematics
    for syst in defs.theory_shape_systs:
        if syst[1] not in added_theory_systs:
            added_theory_systs.append(syst[1])
            cbWithoutQCD.cp().process(syst[0]).AddSyst(cb, syst[1], 'shape', ch.SystMap()(1.))
    
    # Theory rate systematics (not taken from JSON)
    for name,syst in defs.theory_rate_systs.items():
        if not name in added_theory_systs:
            added_theory_systs.append(name)
            cbWithoutQCD.AddSyst(cb, name, syst[0], syst[1])

        
    ### QCD systematics: add a lnN for each bin using the ratio QCD_subtr/QCD_est in the VR
    if options.QCD_systs:
        print('-- Will apply bin-by-bin uncertainties on QCD estimate from ratio in VR --')
        if options.fit_mode == 'shape_direct':
            cb.cp().process(['QCD']).AddSyst(cb, 'bkg_extrap', 'shape', ch.SystMap()(1.))
        if options.fit_mode == 'shape_CR1':
            for i in range(1, Nbins+1):
                lnN = 1 + abs(1 - QCD_VR_ratios[i-1])
                cb.cp().bin(['SR']).process(['QCD_bin_{}'.format(i)]).AddSyst(cb, 'QCD_shape_bin_{}'.format(i), 'lnN', ch.SystMap()(lnN))

    extraStrForQCD = ''
    # To define nuisance group with all QCD parameters
    paramListQCD = []
    
    if options.fit_mode == 'shape_CR1':
        ### QCD estimate: fit shape from CR1, normalisation floating
        extraStrForQCD += 'scale_ratio_QCD_CR1_SR extArg 1. [0.,2.]\n'
        paramListQCD.append('scale_ratio_QCD_CR1_SR')
        
        for i in range(1, Nbins+1):
            extraStrForQCD += 'yield_QCD_SR_bin_{0} rateParam SR QCD_bin_{0} 1. [0.,2.]\n'.format(i)
            paramListQCD.append('yield_QCD_SR_bin_{}'.format(i))
    
        for i in range(1, Nbins+1):
            extraStrForQCD += 'yield_QCD_CR1_bin_{0} rateParam CR1 QCD_bin_{0} (@0*@1) scale_ratio_QCD_CR1_SR,yield_QCD_SR_bin_{0}\n'.format(i)
        
        if options.QCD_systs:
            for i in range(1, Nbins+1):
                paramListQCD.append('QCD_shape_bin_{}'.format(i))
    
    elif options.fit_mode == 'abcd':
        ### QCD estimate: add the rate params for each bin in the CR1, CR2 and VR
        ### The yield in the SR is then expressed as CR1*VR/CR2
        for i in range(1, Nbins+1):
            extraStrForQCD += 'yield_QCD_CR1_bin_{0} rateParam CR1 QCD_bin_{0} 1. [0.,2.]\n'.format(i)
            extraStrForQCD += 'yield_QCD_CR2_bin_{0} rateParam CR2 QCD_bin_{0} 1. [0.,2.]\n'.format(i)
            extraStrForQCD += 'yield_QCD_VR_bin_{0} rateParam VR QCD_bin_{0} 1. [0.,2.]\n'.format(i)
        
            extraStrForQCD += 'yield_QCD_SR_bin_{0} rateParam SR QCD_bin_{0} (@0*@1/@2) yield_QCD_VR_bin_{0},yield_QCD_CR1_bin_{0},yield_QCD_CR2_bin_{0}\n'.format(i)
            
            # We don't want the SR parameter in there, since it's dependent on the others?
            paramListQCD.append('yield_QCD_CR1_bin_{}'.format(i))
            paramListQCD.append('yield_QCD_CR2_bin_{}'.format(i))
            paramListQCD.append('yield_QCD_VR_bin_{}'.format(i))
    
    cb.AddDatacardLineAtEnd(extraStrForQCD)

    # Define systematic groups
    syst_groups = {
            "theory": added_theory_systs,
            "exp": added_exp_systs,
            "QCD": paramListQCD,
            "extern": defs.externalised_nuisances,
        }

    def getNuisanceGroupString(groups):
        m_str = ""
        for g in groups:
            m_str += g + ' group = '
            for sys in groups[g]:
                m_str += sys + ' '
            m_str += '\n'
        return m_str

    cb.AddDatacardLineAtEnd(getNuisanceGroupString(syst_groups))

    cb.cp().ExtractShapes(processed_shapes, '$BIN/$PROCESS', '$BIN/$PROCESS_$SYSTEMATIC')

    if options.bbb:
        print('-- Will add bin-by-bin uncertainties for MC statistics --')
        # MC statistics - has to be done after the shapes have been extracted!
        # bbb_bkg = ch.BinByBinFactory().SetVerbosity(5)
        # bbb_bkg.SetAddThreshold(0.05).SetMergeThreshold(0.5).SetFixNorm(False)
        # bbb_bkg.MergeBinErrors(cb.cp().backgrounds())
        # bbb_bkg.AddBinByBin(cb.cp().backgrounds(), cb)

        # bbb_sig = ch.BinByBinFactory().SetVerbosity(5).SetAddThreshold(0.2).SetFixNorm(False)
        # bbb_sig.AddBinByBin(cbWithoutQCD.cp().signals(), cb)

        # Use combine internal BBB (default: BB lite, merging everything for sig & bkg separately?)
        cb.AddDatacardLineAtEnd("* autoMCStats 0 0 1\n")
    
    output_dir = options.output

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    datacard = os.path.join(output_dir, 'datacard.dat')
    output_shapes = os.path.join(output_dir, 'shapes.root')
        
    cb.WriteDatacard(datacard, output_shapes)

    initWorkSpace = """
#!/bin/bash

if [[ ! -f workspace.root ]]; then
    text2workspace.py datacard.dat -o workspace.root
fi

RMIN=0.
RMAX=2.0
NPOINTS=50
FIT_OPT=( --freezeNuisanceGroups=extern --robustFit=1 --setRobustFitAlgo Minuit2,Minos --setRobustFitStrategy 2 )
"""

    def createScript(content, filename):
        script_path = os.path.join(output_dir, filename)
        with open(script_path, 'w') as f:
            f.write(initWorkSpace)
            f.write(script)
        # make script executable
        st = os.stat(script_path)
        os.chmod(script_path, st.st_mode | stat.S_IEXEC)

    # Script: simple fits
    script = """
combine -M MultiDimFit -d workspace.root --rMin $RMIN --rMax $RMAX --expectSignal=1 -t -1 --algo singles --autoBoundsPOIs "*" "${FIT_OPT[@]}"
# combine -M FitDiagnostics -d workspace.root --rMin $RMIN --rMax $RMAX -t -1 --expectSignal=1 --saveShapes --saveNormalizations --saveWithUncertainties --plots "${FIT_OPT[@]}"
#combine -M MultiDimFit -d workspace.root --rMin $RMIN --rMax $RMAX --expectSignal=1 -t 1000 --toysFrequentist > /dev/null
"""
    createScript(script, 'do_fit.sh')


    # Script: plots of NLL vs. r for different uncertainties
    script = """
RMIN=0.6
RMAX=1.6
combine -M MultiDimFit --algo grid --points $NPOINTS --rMin $RMIN --rMax $RMAX -t -1 --expectSignal=1 -n _nominal workspace.root "${FIT_OPT[@]}"
# combine -M MultiDimFit --algo grid --points $NPOINTS --rMin $RMIN --rMax $RMAX -t -1 --expectSignal=1 -n _theory --freezeNuisanceGroups theory workspace.root "${FIT_OPT[@]}"
combine -M MultiDimFit --algo grid --points $NPOINTS --rMin $RMIN --rMax $RMAX -t -1 --expectSignal=1 -n _stat -S 0 workspace.root "${FIT_OPT[@]}"
# combine -M MultiDimFit --algo grid --points $NPOINTS --rMin $RMIN --rMax $RMAX --expectSignal=1 -n stat --freezeParameters all --fastScan workspace.root
plot1DScan.py higgsCombine_nominal.MultiDimFit.mH120.root --others 'higgsCombine_stat.MultiDimFit.mH120.root:Freeze all:2' --breakdown syst,stat
# plot1DScan.py higgsCombine_nominal.MultiDimFit.mH120.root --others 'higgsCombine_theory.MultiDimFit.mH120.root:Freeze theory:4' 'higgsCombine_stat.MultiDimFit.mH120.root:Freeze all:2' --breakdown theory,syst,stat

#combine -M MultiDimFit --algo grid --points $NPOINTS --rMin $RMIN --rMax $RMAX -t -1 --expectSignal=1 -n _freeze_jet workspace.root --freezeParameters 'rgx{CMS_.*_j$}'
#plot1DScan.py higgsCombine_freeze_jet.MultiDimFit.mH120.root --output scan_freeze_jet

#combine -M MultiDimFit --algo grid --points $NPOINTS --rMin $RMIN --rMax $RMAX -t -1 --expectSignal=1 -n _freeze_qg workspace.root --freezeParameters CMS_qg_Weight
#plot1DScan.py higgsCombine_freeze_qg.MultiDimFit.mH120.root --output scan_freeze_qg

#combine -M MultiDimFit --algo grid --points $NPOINTS --rMin $RMIN --rMax $RMAX -t -1 --expectSignal=1 -n _freeze_btag workspace.root --freezeParameters 'rgx{.*btag.*}'
#plot1DScan.py higgsCombine_freeze_btag.MultiDimFit.mH120.root --output scan_freeze_btag

#combine -M MultiDimFit --algo grid --points $NPOINTS --rMin $RMIN --rMax $RMAX -t -1 --expectSignal=1 -n _freeze_theory workspace.root --freezeNuisanceGroups theory
#plot1DScan.py higgsCombine_freeze_theory.MultiDimFit.mH120.root --output scan_freeze_theory

#combine -M MultiDimFit --algo grid --points $NPOINTS --rMin $RMIN --rMax $RMAX -t -1 --expectSignal=1 -n _freeze_exp workspace.root --freezeNuisanceGroups exp
#plot1DScan.py higgsCombine_freeze_exp.MultiDimFit.mH120.root --output scan_freeze_exp
    """
    createScript(script, 'do_DeltaNLL_plot.sh')


    # Script: impacts signal injected
    script = """
mkdir impacts
pushd impacts

combineTool.py -M Impacts -d ../workspace.root -t -1 -m 120 --rMin $RMIN --rMax $RMAX --expectSignal=1 --doInitialFit "${FIT_OPT[@]}"
combineTool.py -M Impacts -d ../workspace.root -t -1 -m 120 --rMin $RMIN --rMax $RMAX --expectSignal=1 --doFits --parallel 6 "${FIT_OPT[@]}"
combineTool.py -M Impacts -d ../workspace.root -m 120 -o impacts_signal_injected.json
plotImpacts.py -i impacts_signal_injected.json -o impacts_signal_injected

popd
    """
    createScript(script, 'do_impacts_signal_injected.sh')


    # Script: plots of NLL vs. ALL QCD rate parameters
    script = """
function scan_param() {
    combine -M MultiDimFit --algo grid --points 50 -n _$1 ../workspace.root --setParameters r=1 -t -1 --setParameterRanges r=0,2:$1=0.5,1.5 --redefineSignalPOIs $1 "${FIT_OPT[@]}"
    plot1DScan.py higgsCombine_$1.MultiDimFit.mH120.root --output scan_$1 --POI $1
}
export -f scan_param # needed for parallel

mkdir QCD_scans
pushd QCD_scans
SHELL=/bin/bash parallel --gnu -j 6 scan_param ::: %s
popd
""" % " ".join(syst_groups['QCD'])
    createScript(script, 'do_QCD_scans.sh')


def addRateSystematics(cb, json_path, sub_folder=None):
    # JSON is encoded as UTF8 by default, and that messes with the combineHarvester bindings
    def ascii_encode_dict(data):
        def ascii_encode(x):
            if isinstance(x, unicode): return x.encode('ascii')
            else: return x
        return dict(map(ascii_encode, pair) for pair in data.items())

    with open(json_path) as _f:
        systs = json.load(_f, object_hook=ascii_encode_dict)

    if sub_folder is not None:
        systs = systs[sub_folder]

    # Load all systematics in easier order
    # newSysts[systematic] = [ (cat, proc, (down, up)), ... ]
    newSysts = {}
    for cat in systs.keys():
        for sys in systs[cat].keys():
            sys_dict = newSysts.setdefault(sys, [])
            proc_up = set(systs[cat][sys]["Up"].keys())
            proc_down = set(systs[cat][sys]["Down"].keys())
            assert(proc_up == proc_down)
            for proc in proc_up:
                value_up = systs[cat][sys]["Up"][proc]
                value_down = systs[cat][sys]["Down"][proc]
                sys_dict.append( (cat, proc, (value_down, value_up)) )

    # print(newSysts)

    for name, syst in newSysts.items():
        cm = ch.SystMap('bin', 'process')
        for entry in syst:
            cm = cm([entry[0]], [entry[1]], entry[2])
        cb.cp().AddSyst(cb, name, 'lnN', cm)

    return newSysts.keys()
                
    

if __name__ == '__main__':
    main()
