#! /usr/bin/env python
# coding: utf-8

"""
Module for pre-processing SPHERE/IFS data using VIP.
"""

__author__ = 'V. Christiaens'
__all__ = ['preproc_IFS']

from ast import literal_eval
from csv import reader
from json import load
from multiprocessing import cpu_count
from os import system, listdir
from os.path import isfile, isdir
from pdb import set_trace

import matplotlib.pyplot as plt
import numpy as np
from astropy.io import fits
from matplotlib import use as mpl_backend
from matplotlib.colors import LogNorm
from pandas.io.parsers.readers import read_csv

from hciplot import plot_frames
from vcal import __path__ as vcal_path
from vip_hci.fits import open_fits, open_header, write_fits
from vip_hci.fm import normalize_psf, find_nearest
from vip_hci.metrics import stim_map, inverse_stim_map, peak_coordinates, snr
from vip_hci.preproc import (cube_fix_badpix_clump, cube_recenter_2dfit,
                             cube_recenter_dft_upsampling, cube_shift,
                             cube_detect_badfr_pxstats,
                             cube_detect_badfr_ellipticity,
                             cube_detect_badfr_correlation,
                             frame_center_satspots, cube_recenter_satspots,
                             cube_recenter_radon, cube_recenter_via_speckles,
                             frame_shift, cube_crop_frames, frame_crop,
                             cube_derotate, find_scal_vector, cube_subsample,
                             cube_rescaling)
from vip_hci.var import frame_filter_lowpass, get_annulus_segments, mask_circle, frame_center, cart_to_pol
from ..utils.utils_preproc import scaling_by_satspots

mpl_backend("agg")


def preproc_IFS(params_preproc_name='VCAL_params_preproc_IFS.json',
                params_calib_name='VCAL_params_calib.json') -> None:
    """
    Preprocessing of SPHERE/IFS data using preproc parameters provided in 
    json file.
    
    Input:
    ******
    params_preproc_name: str, opt
        Full path + name of the json file containing preproc parameters.
    params_calib_name: str, opt
        Full path + name of the json file containing calibration parameters.
        
    Output:
    *******
    None. All preprocessed products are written as fits files, and can then be 
    used for post-processing.
    
    """
    plt.style.use('default')
    with open(params_preproc_name, 'r') as read_file_params_preproc:
        params_preproc = load(read_file_params_preproc)
    with open(params_calib_name, 'r') as read_file_params_calib:
        params_calib = load(read_file_params_calib)

    with open(vcal_path[0] + "/instr_param/sphere.json", 'r') as instr_param_file:
        instr_cst = load(instr_param_file)

    #**************************** PARAMS TO BE ADAPTED ****************************
    path = params_calib['path']
    inpath = path+"IFS_reduction/1_calib_esorex/calib/"
    label_test = params_preproc.get('label_test', '')
    outpath = path+"IFS_reduction/2_preproc_vip{}/".format(label_test)
    nd_filename = vcal_path[0][:-4]+"Static/SPHERE_CPI_ND.dat"  # FILE WITH TRANSMISSION OF NEUTRAL DENSITY FILTER
    use_cen_only = params_preproc.get('use_cen_only', 0)

    sky = params_calib['sky']
    if sky:
        sky_pre = "skycorr_"
    else:
        sky_pre = ""
    mode = params_calib["mode"]

    ## note: for CrA9, no need for PSF because the OBJ ones are not saturated 

    # parts of pipeline
    to_do = params_preproc.get('to_do',{1,2,3,4,5,6,7,8})  # parts of pre-processing to be run.
    #1. crop odd + bad pix corr
    #   a. with provided mask (static) => iterative because clumps
    #   b. sigma filtering (cosmic rays) => non-iterative
    #2. Recentering
    #3. final crop
    #4. combine all cropped cubes + compute derot_angles [bin if requested]
    #5. bad frame rejection
    #   a. Plots before rejection
    #   b. Rejection
    #   c. Plots after rejection
    #6. FWHM + unsat flux (PSF)
    #   a. Gaussian
    #   b. Airy
    #7. final ADI cubes writing - pick one from step 5
    overwrite = params_preproc.get('overwrite',[1]*8) # list of bools corresponding to parts of pre-processing to be run again even if files already exist. Same order as to_do
    debug = params_preproc.get('debug',0) # whether to print more info - useful for debugging
    save_space = params_preproc['save_space'] # whether to progressively delete intermediate products as new products are calculated (can save space but will make you start all over from the beginning in case of bug)
    plot = params_preproc['plot']
    imlib = params_preproc.get("imlib", "vip-fft")  # image processing library to be used
    nproc = params_preproc.get('nproc', int(cpu_count()/2))  # number of processors to use - can also be set to cpu_count()/2 for efficiency

    if isinstance(overwrite, (int, bool)):
        overwrite = [overwrite]*8

    # OBS
    coro = params_preproc['coro']  # whether the observations were coronagraphic or not
    coro_sz = params_preproc['coro_sz']  # if coronagraphic, provide radius of the coronagraph in pixels (check header for size in mas and convert)

    # preprocessing options
    rec_met = params_preproc['rec_met']    # recentering method. choice among {"gauss_2dfit", "moffat_2dfit", "dft_nn", "satspots", "radon", "speckle"} # either a single string or a list of string to be tested. If not provided will try both gauss_2dfit and dft. Note: "nn" stand for upsampling factor, it should be an integer (recommended: 100)
    rec_met_psf = params_preproc['rec_met_psf']
    sigfactor = params_preproc['sigfactor']

    badfr_criteria = params_preproc['badfr_crit_names']
    badfr_criteria_psf = params_preproc['badfr_crit_names_psf']
    badfr_crit = params_preproc['badfr_crit']
    badfr_crit_psf = params_preproc['badfr_crit_psf']
    badfr_idx = params_preproc.get('badfr_idx',[[],[],[]])
    max_bpix_nit = params_preproc.get('max_bpix_nit', 10)

    #******************** PARAMS LIKELY GOOD AS DEFAULT ***************************

    # First run  dfits *.fits |fitsort DET.SEQ1.DIT INS1.FILT.NAME INS1.OPTI2.NAME DPR.TYPE INS4.COMB.ROT
    # then adapt below
    #filters = ["K1","K2"] #DBI filters
    #filters_lab = ['_left','_right']
    #lbdas = np.array([2.11,2.251])
    #n_z = lbdas.shape[0]
    diam = instr_cst.get('diam',8.1)
    plsc = params_preproc.get('plsc',0.00746)  # arcsec/pixel # Maire 2016
    #print("Resel: {:.2f} / {:.2f} px (K1/K2)".format(resel[0],resel[1]))

    # Systematic errors (cfr. Maire et al. 2016)
    pup_off = instr_cst.get('pup_off',135.99)
    TN = instr_cst.get('TN',-1.75)  # pm0.08 deg
    ifs_off = params_preproc.get('ifs_off',-100.48)              # for ifs data: -100.48 pm 0.13 deg # for IRDIS: 0
    #scal_x_distort = instr_cst.get('scal_x_distort',1.0059)
    #scal_y_distort = instr_cst.get('scal_y_distort',1.0011)
    mask_scal = params_preproc.get('mask_scal',[0.15,0])

    # preprocessing options
    idx_test_cube = params_preproc.get('idx_test_cube',[0,0,0])
    if isinstance(idx_test_cube,int):
        idx_test_cube = [idx_test_cube]*3
    cen_box_sz = params_preproc.get('cen_box_sz', [31, 21, 31])  # size of the subimage for centering fits
    if isinstance(cen_box_sz, int):
        cen_box_sz = [cen_box_sz]*3
    true_ncen = params_preproc['true_ncen']# number of points in time to use for interpolation of center location in OBJ cubes based on location inferred in CEN cubes. Min value: 2 (set to 2 even if only 1 CEN cube available). Important: this is not necessarily equal to the number of CEN cubes (e.g. if there are 3 CEN cubes, 2 before the OBJ sequence and 1 after, true_ncen should be set to 2, not 3)
    scaling_from_satspots = params_preproc.get('scaling_from_satspots', 1)
    #distort_corr = params_preproc.get('distort_corr',True)
    bp_crop_sz = params_preproc.get('bp_crop_sz',261)         # crop size before bad pix correction for OBJ, PSF and CEN
    final_crop_sz = params_preproc.get('final_crop_sz',[257,256]) #361 => 2.25'' radius; but better to keep it as large as possible and only crop before post-processing. Here we just cut the useless edges (100px on each side)
    final_crop_sz_psf = params_preproc.get('final_crop_sz_psf', 17)  # 51 => 0.25'' radius (~3.5 FWHM)
    psf_model = params_preproc.get('psf_model',"gauss") #'airy' #model to be used to measure FWHM and flux. Choice between {'gauss', 'moff', 'airy'}
    bin_fac = params_preproc.get('bin_fac',1)  # binning factors for final cube. If the cube is not too large, do not bin.
    # SDI
    first_good_ch = params_preproc.get('first_good_ch',0)
    last_good_ch = params_preproc.get('last_good_ch',39)
    n_med_sdi = params_preproc.get('n_med_sdi',[1,2,3,4,5]) # number of channels to combine at beginning and end

    # output names
    final_cubename = params_preproc.get('final_cubename', 'final_cube_ASDI')
    final_lbdaname = params_preproc.get('final_lbdaname', "lbdas.fits")
    final_anglename = params_preproc.get('final_anglename', 'final_derot_angles')
    final_psfname = params_preproc.get('final_psfname', 'final_psf_med')
    final_fluxname = params_preproc.get('final_fluxname','final_flux')
    final_fwhmname = params_preproc.get('final_fwhmname','final_fwhm')
    final_scalefac_name = params_preproc.get('final_scalefacname', 'final_scale_fac')
    ## norm output names
    if final_cubename.endswith(".fits"):
        final_cubename = final_cubename[:-5]
    if final_anglename.endswith(".fits"):
        final_anglename = final_anglename[:-5]
    if final_psfname.endswith(".fits"):
        final_psfname = final_psfname[:-5]
    if final_fluxname.endswith(".fits"):
        final_fluxname = final_fluxname[:-5]
    if final_fwhmname.endswith(".fits"):
        final_fwhmname = final_fwhmname[:-5]
    if final_scalefac_name.endswith(".fits"):
        final_scalefac_name = final_scalefac_name[:-5]
    final_cubename_norm = final_cubename+"_norm"
    final_psfname_norm = final_psfname+"_norm"
    # TO DO LIST:

    #1. crop odd + bad pix corr
    #   a. with provided mask (static) => iterative because clumps
    #   b. sigma filtering (cosmic rays) => non-iterative
    #2. Recenter
    #3. final crop
    #4. combine all cropped cubes + compute derot_angles [bin if requested]
    #5. bad frame rejection
    #   a. Plots before rejection
    #   b. Rejection
    #   c. Plots after rejection
    #6. FWHM + unsat flux (PSF)
    #   a. Gaussian
    #   b. Airy
    #7. final ADI cubes writing - pick one from step 5


    # List of OBJ and PSF files
    dico_lists = {}
    with open(path + "dico_files.csv", 'r') as csvfile:
        csv_file = reader(csvfile)
        for row in csv_file:
            dico_lists[row[0]] = literal_eval(row[1])
    csvfile.close()

    prefix = [sky_pre+"SPHER",sky_pre+"psf_SPHER",sky_pre+"cen_SPHER"]
    #if len(dico_lists['sci_list_ifs'])>0:
    #    prefix.append(sky_pre+"SPHER") #prefix for OBJ, PSF and CEN. Remove the last one if non-coronagraphic!
    #if len(dico_lists['psf_list_ifs'])>0:
    #prefix.append(sky_pre+"psf_SPHER")
    #if len(dico_lists['cen_list_ifs'])>0:
    #prefix.append(sky_pre+"SPHER")

    # SEPARATE LISTS FOR OBJ AND PSF
    OBJ_IFS_list = [x[:-5] for x in listdir(inpath) if x.startswith(prefix[0])]  # don't include ".fits"
    OBJ_IFS_list.sort()
    nobj = len(OBJ_IFS_list)
    obj_psf_list = [OBJ_IFS_list]
    labels = ['']
    labels2 = ['obj']
    final_crop_szs = [final_crop_sz]

    PSF_IFS_list = [x[:-5] for x in listdir(inpath) if x.startswith(prefix[1])]  # don't include ".fits"
    npsf = len(PSF_IFS_list)
    if npsf>0:
        PSF_IFS_list.sort()
        obj_psf_list.append(PSF_IFS_list)
        labels.append('_psf')
        labels2.append('psf')
        final_crop_szs.append(final_crop_sz_psf)

    CEN_IFS_list = [x[:-5] for x in listdir(inpath) if x.startswith(prefix[2])]  # don't include ".fits"
    ncen = len(CEN_IFS_list)
    if ncen > 0:
        CEN_IFS_list.sort()
        obj_psf_list.append(CEN_IFS_list)
        labels.append('_cen')
        labels2.append('cen')
        final_crop_szs.append(final_crop_sz)  # add the same crop for CEN as for OBJ

    if isinstance(n_med_sdi, int):
        n_med_sdi = [n_med_sdi]

    if bool(to_do):

        if not isdir(outpath):
            system("mkdir "+outpath)

        # Extract info from example files
        if not use_cen_only:
            cube, header = open_fits(inpath+OBJ_IFS_list[0]+".fits", header=True, verbose=debug)
        else:  # case there are only CEN files
            cube, header = open_fits(inpath+CEN_IFS_list[0]+".fits", header=True, verbose=debug)
        dit_ifs = float(header['HIERARCH ESO DET SEQ1 DIT'])
        #filt1 = header['HIERARCH ESO INS1 FILT NAME']
        #filt2 = header['HIERARCH ESO INS1 OPTI2 NAME']
        lbda_0 = float(header['CRVAL3'])
        delta_lbda = float(header['CD3_3'])
        n_z = cube.shape[0]
        ori_sz = cube.shape[-1]
        lbdas = np.linspace(lbda_0, lbda_0+(n_z-1)*delta_lbda, n_z)
        nd_filter_SCI = header['HIERARCH ESO INS4 FILT2 NAME'].strip()

        nd_file = read_csv(nd_filename, sep="   ", comment='#', engine="python",
                              header=None, names=['wavelength', 'ND_0.0', 'ND_1.0', 'ND_2.0', 'ND_3.5'])
        nd_wavelen = nd_file['wavelength']
        try:
            nd_transmission_SCI = nd_file[nd_filter_SCI]
        except:
            nd_transmission_SCI = [1]*len(nd_wavelen)

        nd_trans = [nd_transmission_SCI]

        if npsf > 0:
            header = open_header(inpath+PSF_IFS_list[0]+'.fits')
            dit_psf_ifs = float(header['HIERARCH ESO DET SEQ1 DIT'])
            nd_filter_PSF = header['HIERARCH ESO INS4 FILT2 NAME'].strip()
            try:
                nd_transmission_PSF = nd_file[nd_filter_PSF]
            except:
                nd_transmission_PSF = [1]*len(nd_wavelen)

            nd_trans.append(nd_transmission_PSF)

        if not coro and not npsf:  # OBJ will be used as PSF
            final_crop_szs.append(final_crop_sz_psf)

        # Check that transmission is correct
        if ncen > 0:
            header = open_header(inpath+CEN_IFS_list[0]+'.fits')
            nd_filter_CEN = header['HIERARCH ESO INS4 FILT2 NAME'].strip()
            try:
                nd_transmission_CEN = nd_file[nd_filter_CEN]
            except:
                nd_transmission_CEN = [1]*len(nd_wavelen)
            nd_trans.append(nd_transmission_CEN)

        resels = lbdas*0.206265/(plsc*diam)
        max_resel = np.amax(resels)
        scale_list = max(lbdas)/lbdas

        if not isfile(outpath+final_lbdaname):
            write_fits(outpath+final_lbdaname,lbdas, verbose=debug)

        #********************************* BPIX CORR ******************************
        if 1 in to_do:
            print(f"\n************* 1. BPIX CORRECTION (using {nproc} CPUs) *************\n", flush=True)
            # OBJECT + PSF + CEN (if available)
            for file_list in obj_psf_list:
                for fi, filename in enumerate(file_list):
                    if fi == 0:
                        full_output = True
                    else:
                        full_output = False
                    if not isfile(outpath+"{}_1bpcorr.fits".format(filename)) or overwrite[0]:
                        cube, header = open_fits(inpath+filename, header=True, verbose=debug)
                        mask = open_fits(inpath+filename, n=1, verbose=debug)  # mask from esorex showing locations of empty data
                        cube[mask == 2] = 0  # set values of 2 (empty data) in the mask to 0 in the image
                        if cube.shape[1] % 2 == 0 and cube.shape[2] % 2 == 0:
                            cube = cube[:, 1:, 1:]
                        if 0 < bp_crop_sz < cube.shape[1]:
                            cube = cube_crop_frames(cube, bp_crop_sz, verbose=debug)
                        header["NAXIS1"] = cube.shape[1]
                        header["NAXIS2"] = cube.shape[2]
                        cube = cube_fix_badpix_clump(cube, bpm_mask=None, cy=None, cx=None, fwhm=1.2*resels,
                                                     sig=6, protect_mask=0, verbose=full_output,
                                                     half_res_y=False, max_nit=max_bpix_nit, full_output=full_output,
                                                     nproc=nproc)
                        if full_output:
                            write_fits(outpath+filename+"_1bpcorr_bpmap.fits", cube[1], header=header, verbose=debug)
                            cube = cube[0]
                        write_fits(outpath+filename+"_1bpcorr.fits", cube, header=header, verbose=debug)

        #******************************* RECENTERING ******************************
        if use_cen_only:
            obj_psf_list[0] = obj_psf_list[-1]
            OBJ_IFS_list = CEN_IFS_list
        if 2 in to_do:
            print("\n************* 2. RECENTERING *************\n", flush=True)
            for fi, file_list in enumerate(obj_psf_list):  # OBJECT, then PSF (but not CEN)
                if fi == 0:
                    negative = coro
                    rec_met_tmp = rec_met
                    all_mjd = []
                    print("\nRecentering OBJ files\n", flush=True)
                elif fi == 1:
                    negative = False  # PSF frames will never use a coronagraph
                    rec_met_tmp = rec_met_psf
                    print("\nRecentering PSF files\n", flush=True)
                elif fi == 2:  # CEN does not need to be centered and are no longer used, only keep to make master cube
                    if not isfile(outpath + f"{file_list[-1]}_2cen.fits") or overwrite[1]:
                        for fn, filename in enumerate(file_list):
                            system(f"cp {outpath}{filename}_1bpcorr.fits {outpath}{filename}_2cen.fits")
                    print("\nFinished centering\n", flush=True)
                    break
                if not isfile(outpath+f"{file_list[-1]}_2cen.fits") or overwrite[1]:
                    if isinstance(rec_met, list):
                        # PROCEED ONLY ON TEST CUBE
                        cube, head = open_fits(outpath+file_list[idx_test_cube[fi]]+"_1bpcorr.fits", header=True, verbose=debug)
                        mjd = float(head['MJD-OBS'])
                        std_shift = []
                        for ii in range(len(rec_met_tmp)):
                            # find coords of max
                            if fi == 1 or not coro:
                                frame_conv = frame_filter_lowpass(np.median(cube,axis=0),fwhm_size=int(1.2*max_resel))
                                idx_max = np.argmax(frame_conv)
                                yx = np.unravel_index(idx_max,frame_conv.shape)
                                xy = (yx[1],yx[0])
                            else:
                                xy=None
                            if "2dfit" in rec_met_tmp[ii]:
                                cube, y_shifts, x_shifts = cube_recenter_2dfit(cube, xy=xy, fwhm=1.2*max_resel,
                                                                               subi_size=cen_box_sz[fi], model=rec_met_tmp[ii][:-6],
                                                                               nproc=nproc, imlib='opencv', interpolation='lanczos4',
                                                                               offset=None, negative=negative, threshold=False,
                                                                               save_shifts=False, full_output=True, verbose=True,
                                                                               debug=False, plot=plot)
                                std_shift.append(np.sqrt(np.std(y_shifts)**2+np.std(x_shifts)**2))
                                if debug:
                                    write_fits(outpath+"TMP_test_cube_cen{}_{}.fits".format(labels[fi],rec_met_tmp[ii]), cube, verbose=debug)
                            elif "dft" in rec_met_tmp[ii]:
                                # #1 rough centering with peak
                                # _, peak_y, peak_x = peak_coordinates(cube, fwhm=1.2*max_resel, 
                                #                                      approx_peak=None, 
                                #                                      search_box=None,
                                #                                      channels_peak=False)
                                # _, peak_yx_ch = peak_coordinates(cube, fwhm=1.2*max_resel, 
                                #                                  approx_peak=(peak_y, peak_x), 
                                #                                  search_box=31,
                                #                                  channels_peak=True)
                                # cy, cx = frame_center(cube[0])
                                # for zz in range(cube.shape[0]):
                                #     cube[zz] = frame_shift(cube[zz], cy-peak_yx_ch[zz,0], cx-peak_yx_ch[zz,1])
                                #2. alignment with upsampling
                                try:
                                    cube, y_shifts, x_shifts = cube_recenter_dft_upsampling(cube, center_fr1=(xy[1],xy[0]), negative=negative,
                                                                                            fwhm=1.2*max_resel, subi_size=cen_box_sz[fi], upsample_factor=int(rec_met_tmp[ii][4:]),
                                                                                            imlib='opencv', interpolation='lanczos4',
                                                                                            full_output=True, verbose=True, nproc=nproc,
                                                                                            save_shifts=False, debug=False, plot=plot)
                                    std_shift.append(np.sqrt(np.std(y_shifts)**2+np.std(x_shifts)**2))
                                    #3 final centering based on 2d fit
                                    cube_tmp = np.zeros([1,cube.shape[1],cube.shape[2]])
                                    cube_tmp[0] = np.median(cube,axis=0)
                                    _, y_shifts, x_shifts = cube_recenter_2dfit(cube_tmp, xy=xy, fwhm=1.2*max_resel, subi_size=cen_box_sz[fi], model='moff',
                                                                                nproc=nproc, imlib='opencv', interpolation='lanczos4',
                                                                                offset=None, negative=negative, threshold=False,
                                                                                save_shifts=False, full_output=True, verbose=True,
                                                                                debug=False, plot=plot)
                                    for zz in range(cube.shape[0]):
                                        cube[zz] = frame_shift(cube[zz], y_shifts[0], x_shifts[0], imlib=imlib)
                                    if debug:
                                        write_fits(outpath+"TMP_test_cube_cen{}_{}.fits".format(labels[fi],rec_met_tmp[ii]), cube, verbose=debug)
                                except:
                                    y_shifts, x_shifts = np.zeros(cube.shape[0]), np.zeros(cube.shape[0])
                            elif "satspots" in rec_met_tmp[ii]:
                                if ncen == 0:
                                    raise ValueError("No CENTER file found. Cannot recenter based on satellite spots.")
                                # INFER SHIFTS FORM CEN CUBES
                                cen_cube_names = obj_psf_list[-1]
                                mjd_cen = np.zeros(ncen)
                                y_shifts_cen_tmp = np.zeros([ncen,n_z])
                                x_shifts_cen_tmp = np.zeros([ncen,n_z])
                                for cc in range(ncen):
                                    head_cc = open_header(inpath+cen_cube_names[cc])
                                    cube_cen = open_fits(outpath+cen_cube_names[cc]+"_1bpcorr.fits", verbose=debug)
                                    mjd_cen[cc] = float(head_cc['MJD-OBS'])

                                    # SUBTRACT TEST OBJ CUBE (to easily find sat spots)
                                    if not use_cen_only:
                                        cube_cen -= cube
                                    diff = int((ori_sz-bp_crop_sz)/2)
                                    xy_spots = [[105, 169], [171, 181], [117, 103], [183, 115]]
                                    xy_spots_tmp = tuple([(xy_spots[i][0]-diff,xy_spots[i][1]-diff) for i in range(len(xy_spots))])

                                    ### get the MJD time of each cube
                                    _, y_shifts_cen_tmp[cc], x_shifts_cen_tmp[cc], _, _ = cube_recenter_satspots(cube_cen, xy_spots_tmp,
                                                                                                                 subi_size=cen_box_sz[fi],
                                                                                                                 sigfactor=sigfactor, plot=plot,
                                                                                                                 fit_type='moff', lbda=lbdas,
                                                                                                                 debug=False, verbose=True,
                                                                                                                 full_output=True)
                                # median combine results for all MJD CEN bef and all after SCI obs
                                if not use_cen_only:
                                    mjd = float(head['MJD-OBS']) # mjd of first obs
                                    mjd_fin = mjd
                                    if true_ncen > ncen or true_ncen > 4:
                                        raise ValueError("Code not compatible with true_ncen > ncen or true_ncen > 4")
                                    if true_ncen>2:
                                        header_fin = open_header(inpath+OBJ_IFS_list[-1]+'.fits')
                                        mjd_fin = float(header_fin['MJD-OBS'])
                                    elif true_ncen>3:
                                        header_mid = open_header(inpath+OBJ_IFS_list[int(nobj/2)]+'.fits')
                                        mjd_mid = float(header_mid['MJD-OBS'])

                                    unique_mjd_cen = np.zeros(true_ncen)
                                    y_shifts_cen = np.zeros([true_ncen,n_z])
                                    x_shifts_cen = np.zeros([true_ncen,n_z])
                                    y_shifts_cen_err = np.zeros([true_ncen,n_z])
                                    x_shifts_cen_err = np.zeros([true_ncen,n_z])
                                    for cc in range(true_ncen):
                                        if cc == 0:
                                            cond = mjd_cen < mjd
                                        elif cc == true_ncen-1:
                                            cond = mjd_cen > mjd_fin
                                        elif cc == 1 and true_ncen == 3:
                                            cond = (mjd_cen > mjd & mjd_cen < mjd_fin)
                                        elif cc == 1 and true_ncen == 4:
                                            cond = (mjd_cen > mjd & mjd_cen < mjd_mid)
                                        else:
                                            cond = (mjd_cen < mjd_fin & mjd_cen > mjd_mid)

                                        unique_mjd_cen[cc] = np.median(mjd_cen[np.where(cond)])
                                        y_shifts_cen[cc] = np.median(y_shifts_cen_tmp[np.where(cond)][:], axis=0)
                                        x_shifts_cen[cc] = np.median(x_shifts_cen_tmp[np.where(cond)][:], axis=0)
                                        y_shifts_cen_err[cc] = np.std(y_shifts_cen_tmp[np.where(cond)][:], axis=0)
                                        x_shifts_cen_err[cc] = np.std(x_shifts_cen_tmp[np.where(cond)][:], axis=0)

                                    # APPLY THEM TO OBJ CUBES             
                                    ## interpolate based on cen shifts
                                    y_shifts = np.zeros(n_z)
                                    x_shifts = np.zeros(n_z)
                                    for zz in range(n_z):
                                        y_shifts[zz] = np.interp([mjd],unique_mjd_cen,y_shifts_cen[:,zz])
                                        x_shifts[zz] = np.interp([mjd],unique_mjd_cen,x_shifts_cen[:,zz])
                                        cube[zz] = frame_shift(cube[zz], y_shifts[zz], x_shifts[zz], imlib=imlib)
                                    std_shift.append(np.sqrt(np.std(y_shifts)**2+np.std(x_shifts)**2))
                                    if debug:
                                        plt.show()
                                        plt.plot(range(n_z),y_shifts,'ro', label = 'shifts y')
                                        plt.plot(range(n_z),x_shifts,'bo', label = 'shifts x')
                                        plt.show()
                                        write_fits(outpath+"TMP_test_cube_cen{}_{}.fits".format(labels[fi],rec_met_tmp[ii]), cube, verbose=debug)
                            elif "radon" in rec_met_tmp[ii]:
                                cube, y_shifts, x_shifts = cube_recenter_radon(cube, full_output=True, verbose=True, imlib='opencv',
                                                                               interpolation='lanczos4', nproc=nproc)
                                std_shift.append(np.sqrt(np.std(y_shifts)**2+np.std(x_shifts)**2))
                                if debug:
                                    write_fits(outpath+"TMP_test_cube_cen{}_{}.fits".format(labels[fi],rec_met_tmp[ii]), cube, verbose=debug)
                            elif "speckle" in rec_met_tmp[ii]:
                                cube, x_shifts, y_shifts = cube_recenter_via_speckles(cube, cube_ref=None, alignment_iter=5,
                                                                                      gammaval=1, min_spat_freq=0.5, max_spat_freq=3,
                                                                                      fwhm=1.2*max_resel, debug=False, negative=negative,
                                                                                      recenter_median=False, subframesize=20,
                                                                                      imlib='opencv', interpolation='bilinear',
                                                                                      save_shifts=False, plot=plot, nproc=nproc)
                                std_shift.append(np.sqrt(np.std(y_shifts)**2+np.std(x_shifts)**2))
                                if debug:
                                    write_fits(outpath+"TMP_test_cube_cen{}_{}.fits".format(labels[fi],rec_met_tmp[ii]), cube, verbose=debug)
                            else:
                                raise ValueError("Centering method not recognized")

                        # infer the best method from min(stddev of shifts)
                        std_shift = np.array(std_shift)
                        idx_min_shift = np.nanargmin(std_shift)
                        rec_met_tmp = rec_met_tmp[idx_min_shift]
                        print("Best centering method for {}: {}".format(labels[fi],rec_met_tmp), flush=True)
                        print("Press c if satisfied. q otherwise", flush=True)

                    if isinstance(rec_met_tmp, str):  # perform only one centering method as requested by the user
                        final_y_shifts = []
                        final_x_shifts = []
                        final_y_shifts_std = []
                        final_x_shifts_std = []
                        for fn, filename in enumerate(file_list):
                            cube, header = open_fits(outpath+filename+"_1bpcorr.fits", header=True, verbose=debug)
                            if fi == 1 or not coro:
                                cube_med = np.median(cube, axis=0)  # median combine all channels
                                # search in subframe that avoids empty data in the edges
                                y_max, x_max = peak_coordinates(cube_med, fwhm=1,
                                                                approx_peak=frame_center(cube_med), search_box=85)
                                xy = (x_max, y_max)
                                if debug:
                                    print(f"Rough xy position of star: {xy}", flush=True)
                            else:
                                xy = None
                            if "2dfit" in rec_met_tmp:
                                cube, y_shifts, x_shifts = cube_recenter_2dfit(cube, xy=(int(xy[0]), int(xy[1])),
                                                                               fwhm=1.2*max_resel, subi_size=cen_box_sz[fi],
                                                                               model=rec_met_tmp[:-6],  nproc=nproc,
                                                                               imlib='opencv', interpolation='lanczos4',
                                                                               offset=None, negative=negative, threshold=False,
                                                                               save_shifts=False, full_output=True,
                                                                               verbose=True, debug=False, plot=False)
                            elif "dft" in rec_met_tmp:
                                # 1. alignment with upsampling
                                try:
                                    cube_dft = cube_recenter_dft_upsampling(cube, center_fr1=(xy[1], xy[0]), negative=negative,
                                                                            fwhm=1.2*max_resel, subi_size=cen_box_sz[fi],
                                                                            upsample_factor=int(rec_met_tmp[4:]),
                                                                            imlib='opencv', interpolation='lanczos4',
                                                                            full_output=False, verbose=True, nproc=nproc,
                                                                            save_shifts=False, debug=False, plot=False)
                                    # 2. final centering based on 2d fit
                                    cube_tmp = np.zeros([1, cube_dft.shape[-2], cube_dft.shape[-1]], dtype=np.float32)  # needs to have three dimensions
                                    cube_tmp[0] = np.median(cube_dft, axis=0)  # median combine all channels
                                    _, y_shifts, x_shifts = cube_recenter_2dfit(cube_tmp, xy=None, fwhm=1.2*max_resel, subi_size=cen_box_sz[fi], model='moff',
                                                                                nproc=nproc, imlib='opencv', interpolation='lanczos4',
                                                                                offset=None, negative=negative, threshold=False,
                                                                                save_shifts=False, full_output=True, verbose=True,
                                                                                debug=False, plot=False)

                                    if debug:
                                        print(f"dft{int(rec_met_tmp[4:])} + 2dfit centering: xshift: {x_shifts[0]} px, "
                                              f"yshift: {y_shifts[0]} px for cube {filename}_1bpcorr.fits", flush=True)
                                    cube = cube_shift(cube_dft, shift_y=y_shifts[0], shift_x=x_shifts[0], nproc=nproc,
                                                      imlib=imlib)
                                except:
                                    y_shifts, x_shifts = np.zeros(cube.shape[0]), np.zeros(cube.shape[0])
                                    print(f"\nWARNING: DFT upsampling and 2d fit failed for cube {filename}_1bpcorr.fits\n"
                                          f"The shifts for this cube will be set to zero. It is recommended to check\n"
                                          f"these cubes individually to determine the cause.\n")

                            elif "satspots" in rec_met_tmp:
                                # if first file and OBJ, or only CEN
                                if fn == 0 and (fi == 0 or (fi == 2 and use_cen_only)):
                                    if ncen == 0:
                                        raise ValueError("No CENTER file found. Cannot recenter based on satellite spots.")
                                    # INFER SHIFTS FROM CEN CUBES
                                    cen_cube_names = obj_psf_list[-1]
                                    mjd_cen = np.zeros(ncen)
                                    y_shifts_cen_tmp = np.zeros(ncen)
                                    x_shifts_cen_tmp = np.zeros(ncen)
                                    sat_y = np.zeros([ncen, 4])  # four spots per cube
                                    sat_x = np.zeros([ncen, 4])
                                    loD = lbdas * 1e-6 / 8.2 * 180 / np.pi * 3600 / plsc  # 1lambda/D in detector px, rad -> deg -> arcsec -> detector px
                                    factor = 14  # satspots appear at 14lambda/D, from SPHERE manual

                                    # loop over all CEN files and retrieve the location of the satellite spots
                                    for cc in range(ncen):
                                        # first get the MJD time of each cube
                                        cube_cen, head_cc = open_fits(outpath+cen_cube_names[cc]+"_1bpcorr.fits", verbose=debug, header=True)
                                        if cc == 0:
                                            all_cen_sub = np.zeros((ncen, cube_cen.shape[0], cube_cen.shape[-2], cube_cen.shape[-1]))
                                        mjd_cen[cc] = float(head_cc['MJD-OBS'])
                                        waffle_orientation = head_cc['HIERARCH ESO OCS WAFFLE ORIENT']
                                        if waffle_orientation == '+':
                                            orient = abs(ifs_off) * np.pi / 180
                                        elif waffle_orientation == 'x':
                                            orient = abs(ifs_off) * np.pi / 180 + np.pi / 4
                                        ref_xy = frame_center(cube_cen)

                                        spot_xy = np.zeros((len(lbdas), 4, 2))  # fill with predictions
                                        for idx in range(len(lbdas)):
                                            for spot in range(4):
                                                spot_xy[idx][spot][0] = ref_xy[0] + factor * loD[idx] * np.cos(
                                                    orient + np.pi / 2 * spot)
                                                spot_xy[idx][spot][1] = ref_xy[1] + factor * loD[idx] * np.sin(
                                                    orient + np.pi / 2 * spot)

                                        # SUBTRACT NEAREST OBJ CUBE to find sat spots
                                        if not use_cen_only:
                                            mjd_mean = []
                                            for fn_tmp, filename_tmp in enumerate(file_list):
                                                head_tmp = open_header(inpath+OBJ_IFS_list[fn_tmp])
                                                mjd_tmp = float(head_tmp["MJD-OBS"])
                                                mjd_mean.append(mjd_tmp)
                                            m_idx = find_nearest(mjd_mean, mjd_cen[cc])
                                            cube_near = open_fits(outpath+file_list[m_idx]+"_1bpcorr.fits", verbose=debug)
                                            if cube_near.ndim == 4:
                                                cube_near = np.median(cube_near, axis=0)
                                            print(f"\nOBJ cube {file_list[m_idx]}_1bpcorr.fits will be subtracted from "
                                                  f"CEN cube {cen_cube_names[cc]}_1bpcorr.fits\n", flush=True)
                                            all_cen_sub[cc] = cube_cen - cube_near
                                        else:
                                            all_cen_sub[cc] = cube_cen

                                        iterations = 5  # hard code for now, although two seems like enough
                                        for i in range(iterations):
                                            # rescale CEN (and OBJ), median combine to improve satspot flux, find
                                            # satspot center, use new center to improve rescaling
                                            cube_cen_sub = cube_rescaling(cube_cen, scaling_list=scale_list,
                                                                          ref_xy=ref_xy, imlib="opencv")
                                            if not use_cen_only:
                                                # rescale both the sci and cen cubes using theoretical scaling factors
                                                cube_near = cube_rescaling(cube_near, scaling_list=scale_list,
                                                                           ref_xy=ref_xy, imlib="opencv")
                                                cube_cen_sub -= cube_near

                                            cube_cen_sub = np.median(cube_cen_sub, axis=0)  # median collapse channels
                                            # find center location
                                            res = frame_center_satspots(cube_cen_sub, (spot_xy[-1][0], spot_xy[-1][-1], spot_xy[-1][1], spot_xy[-1][2]),
                                                                        subi_size=cen_box_sz[fi], sigfactor=sigfactor,
                                                                        fit_type="moff", verbose=debug, shift=True)

                                            _, y_shifts_cen_tmp[cc], x_shifts_cen_tmp[cc], sat_y[cc], sat_x[cc] = res
                                            ref_xy = (frame_center(cube_cen)[1]-x_shifts_cen_tmp[cc],
                                                      frame_center(cube_cen)[0]-y_shifts_cen_tmp[cc])

                                        if plot and cc == 0:
                                            plt.close("all")
                                            fig = plt.figure(figsize=(8, 8))

                                            ax = fig.add_subplot(111)
                                            ax.imshow(cube_cen_sub, aspect='equal',
                                                      norm=LogNorm(np.nanmax(cube_cen[0]) * 1e-2,
                                                                   vmax=np.nanmax(cube_cen[0]),
                                                                   clip=True), interpolation='nearest',
                                                      cmap="inferno")
                                            ax.set_xlabel('x coordinate [px]')
                                            ax.set_ylabel('y coordinate [px]')
                                            ax.invert_yaxis()
                                            ax.annotate("Rescaled and median combined",
                                                        xy=(10, 420), xycoords='axes pixels', weight='bold',
                                                        color="white")
                                            ax.annotate(
                                                f"Predicted satellite spot positions\nUsing the {waffle_orientation} waffle pattern\n{cen_cube_names[cc]}\nClosest in time science frame has been subtracted",
                                                xy=(10, 10), xycoords='axes pixels', weight='bold',
                                                color="white")

                                            for coords in spot_xy:
                                                x, y = coords.T
                                                ax.scatter(x, y, c="white", alpha=0.2)

                                            plt.savefig(outpath + "Predicted_satspots.pdf", bbox_inches='tight',
                                                        pad_inches=0.01)
                                            plt.close("all")

                                    write_fits(outpath+ "all_cen_subtracted.fits", all_cen_sub, verbose=debug)
                                    # median combine results for all MJD CEN bef and all after SCI obs
                                    header_ini = open_header(inpath+OBJ_IFS_list[0]+'.fits')
                                    mjd = float(header_ini["MJD-OBS"])  # mjd of first obs
                                    mjd_fin = mjd
                                    if true_ncen > ncen or true_ncen > 4:
                                        raise ValueError("Code not compatible with true_ncen > ncen or true_ncen > 4")
                                    if true_ncen>2:
                                        header_fin = open_header(inpath+OBJ_IFS_list[-1]+'.fits')
                                        mjd_fin = float(header_fin['MJD-OBS'])
                                    elif true_ncen>3:
                                        header_mid = open_header(inpath+OBJ_IFS_list[int(nobj/2)]+'.fits')
                                        mjd_mid = float(header_mid['MJD-OBS'])

                                    unique_mjd_cen = np.zeros(true_ncen)
                                    y_shifts_cen = np.zeros([true_ncen, n_z])
                                    x_shifts_cen = np.zeros([true_ncen, n_z])
                                    y_shifts_cen_err = np.zeros([true_ncen, n_z])
                                    x_shifts_cen_err = np.zeros([true_ncen, n_z])
                                    for cc in range(true_ncen):
                                        if cc == 0:
                                            cond = mjd_cen < mjd
                                        elif cc == true_ncen-1:
                                            cond = mjd_cen > mjd_fin
                                        elif cc == 1 and true_ncen == 3:
                                            cond = (mjd_cen > mjd & mjd_cen < mjd_fin)
                                        elif cc == 1 and true_ncen == 4:
                                            cond = (mjd_cen > mjd & mjd_cen < mjd_mid)
                                        else:
                                            cond = (mjd_cen < mjd_fin & mjd_cen > mjd_mid)

                                        unique_mjd_cen[cc] = np.median(mjd_cen[np.where(cond)])
                                        y_shifts_cen[cc] = np.median(y_shifts_cen_tmp[np.where(cond)][:], axis=0)
                                        x_shifts_cen[cc] = np.median(x_shifts_cen_tmp[np.where(cond)][:], axis=0)
                                        y_shifts_cen_err[cc] = np.std(y_shifts_cen_tmp[np.where(cond)][:], axis=0)
                                        x_shifts_cen_err[cc] = np.std(x_shifts_cen_tmp[np.where(cond)][:], axis=0)  # SAVE UNCERTAINTY ON CENTERING
                                    #unc_cen = np.sqrt(np.power(np.amax(y_shifts_cen_err),2)+np.power(np.amax(x_shifts_cen_err),2))
                                    #write_fits(outpath+"Uncertainty_on_centering_sat_spots_px.fits", np.array([unc_cen]))
                                    # if np.amax(x_shifts_cen_err)>3 or np.amax(y_shifts_cen_err)>3:
                                    #     msg = "Warning: large std found for calculated shifts (std_x: {:.1f}, std_y: {:.1f}) px." 
                                    #     msg+= "Make sure CEN cubes and sat spots fits look good."
                                    #     print(msg)
                                    #     pdb.set_trace()

                                if not use_cen_only:  # APPLY THEM TO OBJ CUBES
                                    y_shifts = np.zeros(n_z)
                                    x_shifts = np.zeros(n_z)
                                    mjd = float(header['MJD-OBS'])
                                    all_mjd.append(mjd)
                                    for zz in range(n_z):  # interpolate based on cen shifts
                                        y_shifts[zz] = np.interp(x=[mjd], xp=unique_mjd_cen, fp=y_shifts_cen[:, zz])
                                        x_shifts[zz] = np.interp(x=[mjd], xp=unique_mjd_cen, fp=x_shifts_cen[:, zz])
                                    cube = cube_shift(cube, shift_y=y_shifts, shift_x=x_shifts, nproc=nproc,
                                                      imlib=imlib)

                                    if plot and fn == 0:
                                        # two plots are made here, first shows x-y shifts in each channel, the second
                                        # shows the coordinates of all satellite spots found in each cube and each
                                        # channel
                                        fig, axs = plt.subplots(nrows=2, sharex=True)
                                        fig.suptitle("Shifts inferred from satellite spots")
                                        colors = ["r", "b", "m", "c", "y", "g", "k"]  # different colours for each CEN
                                        # y
                                        for cc in range(true_ncen):
                                            axs[0].errorbar(range(n_z), y_shifts_cen[cc], yerr=y_shifts_cen_err[cc],
                                                            fmt=colors[cc]+"o", label=f"y-shifts cube {cc+1}")
                                        # x
                                        for cc in range(true_ncen):
                                            axs[1].errorbar(range(n_z), x_shifts_cen[cc], yerr=x_shifts_cen_err[cc],
                                                            fmt=colors[cc]+"o", label=f"x-shifts cube {cc+1}")
                                        fig.supxlabel("Channel")
                                        axs[0].set_ylabel("y shift [px]")
                                        axs[1].set_ylabel("x shift [px]")
                                        axs[0].legend(loc="best")
                                        axs[1].legend(loc="best")
                                        axs[0].minorticks_on()
                                        axs[1].minorticks_on()
                                        plt.savefig(outpath+"Satspot_shifts_per_channel.pdf", bbox_inches="tight")
                                        plt.close("all")

                                        fig, ax = plt.subplots()
                                        for cc in range(true_ncen):
                                            ax.scatter(sat_x[cc], sat_y[cc], label=f"Cube {cc+1}", s=6, alpha=0.7)
                                        ax.scatter(cube_cen.shape[-1]/2, cube_cen.shape[-1]/2, color="black", marker="x")
                                        ax.annotate("(Frame Center)", xy=(cube_cen.shape[-1]/2+3, cube_cen.shape[-1]/2+3),
                                                    fontsize=8)
                                        ax.axhline(cube_cen.shape[-1]/2, color="black", ls=":", alpha=0.5)
                                        ax.axvline(cube_cen.shape[-1]/2, color="black", ls=":", alpha=0.5)
                                        ax.minorticks_on()
                                        ax.legend(loc="best")
                                        ax.set_xlabel("Image x-coordinate [px]")
                                        ax.set_ylabel("Image y-coordinate [px]")
                                        plt.savefig(outpath+"Satspot_coordinates.pdf", bbox_inches="tight")
                                        plt.close("all")

                                # scaling factors
                                if fn == 0 and scaling_from_satspots:
                                    spot_xy_predict = np.zeros((len(lbdas), 4, 2))  # fill with predictions
                                    spot_xcenters = np.zeros((len(lbdas), 4))  # measured
                                    spot_ycenters = np.zeros((len(lbdas), 4))  # measured
                                    snr_val = np.zeros((len(lbdas), 4))
                                    snr_channel_mean = np.zeros((ncen, len(lbdas)))
                                    scale_list_measured_mean = np.zeros((ncen, len(lbdas)))

                                    # find all spots
                                    for cc in range(ncen):
                                        img = all_cen_sub[cc]
                                        for idx in range(len(lbdas)):
                                            for spot in range(4):
                                                spot_xy_predict[idx][spot][0] = ref_xy[0] - x_shifts_cen_tmp[cc] + factor * loD[idx] * np.cos(
                                                    orient + np.pi / 2 * spot)
                                                spot_xy_predict[idx][spot][1] = ref_xy[1] - y_shifts_cen_tmp[cc] + factor * loD[idx] * np.sin(
                                                    orient + np.pi / 2 * spot)
                                            if debug:
                                                print(f"Attempting to fit the satellite spot positions in channel {idx}")
                                            _, _, _, spot_ycenters[idx], spot_xcenters[idx] = frame_center_satspots(img[idx],
                                                                                                                    xy=(
                                                                                                                    spot_xy_predict[
                                                                                                                        idx][0],
                                                                                                                    spot_xy_predict[
                                                                                                                        idx][
                                                                                                                        -1],
                                                                                                                    spot_xy_predict[
                                                                                                                        idx][1],
                                                                                                                    spot_xy_predict[
                                                                                                                        idx][
                                                                                                                        2]),
                                                                                                                    subi_size=21,
                                                                                                                    shift=True,
                                                                                                                    fit_type="moff",
                                                                                                                    verbose=False)
                                        coordinates_array = np.column_stack((spot_xcenters.flatten(), spot_ycenters.flatten()))  # stack the columns of x and y coordinates together
                                        coordinates_array = coordinates_array.reshape((len(lbdas), 4, 2))  # reshape to sames as predicted array
                                        # measure SNR, avoid other spots
                                        for idx in range(len(lbdas)):
                                            for spot in range(4):
                                                try:  # to get the SNR, if it fails then set to nan
                                                    snr_val[idx][spot] = snr(img[idx], source_xy=(coordinates_array[idx][spot][0], coordinates_array[idx][spot][1]),
                                                                             fwhm=2 * resels[idx], verbose=False, plot=False,
                                                                             exclude_theta_range=(cart_to_pol(coordinates_array[idx][spot][0],
                                                                                                              coordinates_array[idx][spot][1],
                                                                                         cy=img.shape[-1] / 2,
                                                                                         cx=img.shape[-1] / 2)[1] + 70,
                                                                             cart_to_pol(coordinates_array[idx][spot][0],
                                                                                         coordinates_array[idx][spot][1],
                                                                                         cy=img.shape[-1] / 2,
                                                                                         cx=img.shape[-1] / 2)[1] + 280))
                                                except ZeroDivisionError:
                                                    snr_val[idx][spot] = np.nan
                                        snr_channel_mean[cc] = np.nanmean(snr_val, axis=1)  # mean of each spot
                                        scale_list_measured_mean[cc] = scaling_by_satspots(lbdas, coordinates_array,
                                                                                           snr_channel_mean[cc], snr_thres=8)

                                    snr_channel_mean = np.nanmean(snr_channel_mean, axis=0)  # mean of each cube
                                    scale_list_measured_mean = np.nanmean(scale_list_measured_mean, axis=0)
                                    write_fits(outpath + final_scalefac_name, scale_list_measured_mean, verbose=debug)

                                    if plot:
                                        plt.close("all")
                                        plt.plot(lbdas, snr_val[:, 0], label="top-left")
                                        plt.plot(lbdas, snr_val[:, 1], label="top-right")
                                        plt.plot(lbdas, snr_val[:, 2], label="bottom-left")
                                        plt.plot(lbdas, snr_val[:, 3], label="bottom-right")
                                        plt.plot(lbdas, snr_channel_mean, label="Mean", linestyle="-.", c="grey")
                                        plt.legend()
                                        plt.minorticks_on()
                                        plt.xlabel("Wavelength [µm]")
                                        plt.ylabel("Approx. SNR")
                                        plt.savefig(outpath + "Satspots_SNR.pdf", bbox_inches='tight', pad_inches=0.01)
                                        plt.close("all")

                                        fig, axes = plt.subplots(nrows=1, ncols=2, figsize=(10, 4), dpi=300,
                                                                 constrained_layout=True)

                                        axes[0].scatter(lbdas, scale_list_measured_mean, label="Measured")
                                        axes[0].set_xlabel('Wavelength [µm]')
                                        axes[0].set_ylabel('Scaling Factor')
                                        axes[0].plot(lbdas, max(lbdas) / lbdas, label="Theoretical", c="black",
                                                     linestyle="--", alpha=0.8)
                                        axes[1].scatter(lbdas, scale_list_measured_mean - max(lbdas) / lbdas, label="Deviation")
                                        axes[1].hlines(0, xmin=min(lbdas), xmax=max(lbdas), label="Theoretical",
                                                       color="black", linestyle="--", alpha=0.8)
                                        axes[1].set_xlabel('Wavelength [µm]')
                                        axes[1].set_ylabel('Measured - Theoretical')
                                        axes[0].legend()
                                        axes[0].minorticks_on()
                                        axes[1].legend()
                                        axes[1].minorticks_on()
                                        plt.savefig(outpath + "Scaling_from_satspots.pdf", bbox_inches='tight', pad_inches=0.01)
                                        plt.close("all")

                                    best = np.argsort(snr_channel_mean)[-5:]
                                    write_fits(outpath+"Satspot_SNR_best.fits", best, verbose=debug)
                                    print(f"Five highest SNR channels {best+1} which are {lbdas[best]} µm")

                            elif "radon" in rec_met_tmp:
                                cube, y_shifts, x_shifts, _ = cube_recenter_radon(cube, full_output=True, verbose=True, imlib='opencv',
                                                                                  interpolation='lanczos4', nproc=nproc)
                            elif "speckle" in rec_met_tmp:
                                cube, _, _, x_shifts, y_shifts = cube_recenter_via_speckles(cube, cube_ref=None, alignment_iter=5,
                                                                                            gammaval=1, min_spat_freq=0.5,
                                                                                            max_spat_freq=3,
                                                                                            fwhm=1.2*max_resel, debug=False,
                                                                                            negative=negative,
                                                                                            recenter_median=True, fit_type="ann",
                                                                                            subframesize=cen_box_sz[fi],
                                                                                            plot=False, nproc=nproc,
                                                                                            full_output=True)
                            else:
                                raise ValueError("Centering method not recognized")
                            write_fits(outpath+filename+"_2cen.fits", cube, header=header, verbose=debug)
                            final_y_shifts.append(np.median(y_shifts))
                            final_x_shifts.append(np.median(x_shifts))
                            final_y_shifts_std.append(np.std(y_shifts))
                            final_x_shifts_std.append(np.std(x_shifts))
                    if plot:
                        try:
                            f, (ax1) = plt.subplots(nrows=1, ncols=1, figsize=(15, 10))
                            if fi == 0:
                                ax1.errorbar(all_mjd,final_y_shifts,final_y_shifts_std,fmt='bo',label='y')
                                ax1.errorbar(all_mjd,final_x_shifts,final_x_shifts_std,fmt='ro',label='x')
                            if "satspot" in rec_met_tmp:
                                ax1.errorbar(unique_mjd_cen,np.median(y_shifts_cen,axis=1),np.std(y_shifts_cen,axis=1),
                                             fmt='co',label='y cen')
                                ax1.errorbar(unique_mjd_cen,np.median(x_shifts_cen,axis=1),np.std(x_shifts_cen,axis=1),
                                             fmt='mo',label='x cen')
                            plt.legend(loc='best')
                            plt.savefig(outpath+"Shifts_xy{}_{}.pdf".format(labels[fi],rec_met_tmp),bbox_inches='tight', format='pdf')
                            plt.close('all')
                        except:
                            print('Could not produce shifts vs. time plot.', flush=True)
                            plt.close('all')

            if save_space:
                system(f"rm -f {outpath}*1bpcorr*.fits")

        #******************************* MASTER CUBES ******************************
        if 3 in to_do:
            print("\n************* 3. MASTER CUBES *************\n", flush=True)
            for fi,file_list in enumerate(obj_psf_list):
                if fi == 0 and use_cen_only:
                    continue  # skip OBJ
                if (not isfile(outpath+"1_master_ASDIcube{}.fits".format(labels[fi])) or not isfile(outpath+"1_master_derot_angles.fits") or overwrite[2]):
                    if fi!=1:
                        parang_st = []
                        parang_nd = []
                    for nn, filename in enumerate(file_list):
                        cube, header = open_fits(outpath+filename+"_2cen.fits", header=True, verbose=debug)
                        if nn == 0:
                            master_cube = np.zeros([n_z,len(file_list),cube.shape[-2],cube.shape[-1]], dtype=np.float32)
                        try:
                            master_cube[:,nn] = cube
                        except:
                            set_trace()
                        if fi!=1:
                            parang_st.append(float(header["HIERARCH ESO TEL PARANG START"]))
                            parang_nd_tmp = float(header["HIERARCH ESO TEL PARANG END"])
                            #posang.append(float(header["HIERARCH ESO ADA POSANG"]))
                            if nn> 0:
                                if abs(parang_st[-1]-parang_nd_tmp)>180:
                                    sign_tmp=np.sign(parang_st[-1]-parang_nd_tmp)
                                    parang_nd_tmp=parang_nd_tmp+sign_tmp*360
                            parang_nd.append(parang_nd_tmp)

                    # VERY IMPORTANT WE CORRECT FOR SPECTRAL IMPRINT OF ND FILTER
                    interp_trans = np.interp(lbdas*1000, nd_wavelen, nd_trans[fi])  # file lbdas are in nm
                    if debug:
                        print("transmission correction: ",interp_trans, flush=True)
                    for zz in range(n_z):
                        master_cube[zz] = master_cube[zz]/interp_trans[zz]

                    # IMPORTANT WE DO NOT NORMALIZE BY DIT (anymore!)
                    write_fits(outpath+"1_master_ASDIcube{}.fits".format(labels[fi]), master_cube, #/dits[fi],
                               verbose=debug)

                    if fi!=1:
                        final_derot_angles = []
                        final_par_angles = []
                        # open real ndit from calibration
                        true_ndit = open_fits(inpath + f"../fits/true_ndit_{labels2[fi]}.fits", verbose=debug, precision=int)

                        counter = 0
                        for nn, ndit in enumerate(true_ndit):
                            x = parang_st[counter]
                            y = parang_nd[counter]
                            parang = x + (y - x) * (0.5 + np.arange(ndit)) / ndit
                            final_derot_angles.extend(list(parang + TN + pup_off + ifs_off))
                            final_par_angles.extend(list(parang))
                            counter += ndit
                        write_fits(outpath + "1_master_derot_angles{}.fits".format(labels[fi]), np.array(final_derot_angles), verbose=debug)
                        write_fits(outpath + "1_master_par_angles{}.fits".format(labels[fi]), np.array(final_par_angles), verbose=debug)

        #********************* PLOTS + TRIM BAD FRAMES OUT ************************

        if 4 in to_do:
            print("\n************* 4. PLOTS + TRIM BAD FRAMES OUT *************\n", flush=True)
            for fi, file_list in enumerate(obj_psf_list):
#                    if fi == 1:
#                        dist_lab_tmp = "" # no need for PSF
#                    else:
#                        dist_lab_tmp = dist_lab
                if fi == 0 and use_cen_only:
                    continue
                elif fi == 2 and not use_cen_only: # no need for CEN, except if no OBJ
                    break

                if not coro:
                    fi_tmp = fi
                else:
                    fi_tmp = 1
                # Determine fwhm
                if not isfile(outpath+f"TMP_fwhm{labels[fi_tmp]}.fits") or overwrite[3]:
                    cube = open_fits(outpath+f"1_master_ASDIcube{labels[fi_tmp]}.fits", verbose=debug)
                    fwhm = np.zeros(n_z)
                    # if a list of crops are given, use the last odd one
                    if isinstance(final_crop_sz_psf, list):
                        final_crop_sz_psf_tmp = final_crop_sz_psf[[i for i in range(len(final_crop_sz_psf)) if i % 2 == 0][-1]]
                    else:
                        final_crop_sz_psf_tmp = final_crop_sz_psf
                    # first fit on median
                    for zz in range(n_z):
                        _, _, fwhm[zz] = normalize_psf(np.median(cube[zz], axis=0), fwhm="fit", size=final_crop_sz_psf_tmp,
                                                       threshold=None, mask_core=None, model=psf_model, imlib="opencv",
                                                       interpolation="lanczos4", force_odd=True, full_output=True,
                                                       verbose=debug, debug=False, correct_outliers=True)
                    write_fits(outpath+f"TMP_fwhm{labels[fi_tmp]}.fits", fwhm, verbose=debug)
                else:
                    fwhm = open_fits(outpath+f"TMP_fwhm{labels[fi_tmp]}.fits", verbose=debug)
                fwhm_med = np.median(fwhm)

                cube = open_fits(outpath+f"1_master_ASDIcube{labels[fi]}.fits", verbose=debug)
                nfr = cube.shape[1]

                if fi != 1:
                    badfr_critn_tmp = badfr_criteria
                    badfr_crit_tmp = badfr_crit
                else:
                    badfr_critn_tmp = badfr_criteria_psf
                    badfr_crit_tmp = badfr_crit_psf
                bad_str = "-".join(badfr_critn_tmp)
                if not isfile(outpath+f"2_master{labels[fi]}_ASDIcube_clean_{bad_str}.fits") or overwrite[3]:
                    # OBJECT                 
                    if fi != 1:
                        derot_angles = open_fits(outpath+f"1_master_derot_angles{labels[fi]}.fits", verbose=debug)

                    if len(badfr_idx[fi]) > 0:
                        cube_tmp = np.zeros([cube.shape[0], cube.shape[1]-len(badfr_idx[fi]), cube.shape[2], cube.shape[3]])
                        derot_angles_tmp = np.zeros(cube.shape[1]-len(badfr_idx[fi]))
                        counter = 0
                        for nn in range(cube.shape[1]):
                            if nn not in badfr_idx[fi]:
                                cube_tmp[:,counter] = cube[:,nn]
                                derot_angles_tmp[counter] = derot_angles[nn]
                                counter += 1
                        cube = np.copy(cube_tmp)
                        derot_angles = np.copy(derot_angles_tmp)
                        cube_tmp = None
                        derot_angles_tmp = None

                    final_good_index_list = list(range(cube.shape[1]))

                    # if Satspot_SNR_best.fits exists, read the numbers and use those channels to detect bad frames
                    if isfile(outpath+"Satspot_SNR_best.fits"):
                        channels = open_fits(outpath+"Satspot_SNR_best.fits", verbose=debug)
                        channels = np.array(channels, dtype=int)  # ensure channels are integers
                        print(f"Using five highest SNR channels {channels+1} from satspots for bad frame detection")
                    else:
                        channels = np.arange(0, n_z)
                    for zz in channels:  # zero based in the case of no prior good channels
                        print(f"********** Identifying bad frames using channel {zz+1} ***********\n", flush=True)
                        ngood_fr_ch = len(final_good_index_list)

                        plot_tmp = False
                        if zz == channels[0] or zz == channels[-1] or debug:  # only plot for channel 1&39 or when debug is on
                            plot_tmp = plot

                        # Rejection based on pixel statistics
                        if "stat" in badfr_critn_tmp:
                            idx_stat = badfr_critn_tmp.index("stat")
                            # Default parameters
                            mode = "circle"
                            rad = int(2*fwhm_med)
                            width = 0
                            window = None
                            if coro and fi == 0:
                                mode = "annulus"
                                rad = int(coro_sz+1)
                                width = int(fwhm_med*2)
                                window = int(len(cube[zz])/10)
                            top_sigma = 1.0
                            low_sigma = 1.0
                            # Update if provided
                            if "mode" in badfr_crit_tmp[idx_stat].keys():
                                mode = badfr_crit_tmp[idx_stat]["mode"]
                            if "rad" in badfr_crit_tmp[idx_stat].keys():
                                rad = int(badfr_crit_tmp[idx_stat]["rad"]*fwhm_med)
                            if "width" in badfr_crit_tmp[idx_stat].keys():
                                width = int(badfr_crit_tmp[idx_stat]["width"]*fwhm_med)
                            if "thr_top" in badfr_crit_tmp[idx_stat].keys():
                                top_sigma = badfr_crit_tmp[idx_stat]["thr_top"]
                            if "thr_low" in badfr_crit_tmp[idx_stat].keys():
                                low_sigma = badfr_crit_tmp[idx_stat]["thr_low"]
                            if "window" in badfr_crit_tmp[idx_stat].keys():
                                window = badfr_crit_tmp[idx_stat]["window"]
                            good_index_list, bad_index_list = cube_detect_badfr_pxstats(cube[zz], mode=mode, in_radius=rad,
                                                                                        width=width, top_sigma=top_sigma,
                                                                                        low_sigma=low_sigma, window=window,
                                                                                        plot=plot_tmp, verbose=debug)
                            if plot_tmp:
                                plt.savefig(outpath+f"badfr_stat_plot{labels[fi]}_ch{zz}.pdf", bbox_inches="tight")
                            final_good_index_list = [idx for idx in list(good_index_list) if idx in final_good_index_list]

                        # Rejection based on ellipticity
                        if "ell" in badfr_critn_tmp:
                            idx_ell = badfr_critn_tmp.index("ell")
                            # default params
                            roundhi = 0.2
                            roundlo = -0.2
                            crop_sz = 10
                            # Update if provided
                            if "roundhi" in badfr_crit_tmp[idx_ell].keys():
                                roundhi = badfr_crit_tmp[idx_ell]["roundhi"]
                            if "roundlo" in badfr_crit_tmp[idx_ell].keys():
                                roundlo = badfr_crit_tmp[idx_ell]["roundlo"]
                            if "crop_sz" in badfr_crit_tmp[idx_ell].keys():
                                crop_sz = badfr_crit_tmp[idx_ell]["crop_sz"]
                            crop_size = int(crop_sz*fwhm[zz])
                            if not crop_sz % 2:
                                crop_size += 1
                            crop_sz = min(cube[zz].shape[1]-2, crop_sz)
                            good_index_list, bad_index_list = cube_detect_badfr_ellipticity(cube[zz], fwhm=fwhm[zz],
                                                                                            crop_size=crop_sz,
                                                                                            roundlo=roundlo, roundhi=roundhi,
                                                                                            plot=plot_tmp, verbose=debug)
                            if plot_tmp:
                                plt.savefig(outpath+f"badfr_ell_plot{labels[fi]}_ch{zz}.pdf", bbox_inches="tight")
                            final_good_index_list = [idx for idx in list(good_index_list) if idx in final_good_index_list]

                        # Rejection based on correlation to the median
                        if "corr" in badfr_critn_tmp:
                            idx_corr = badfr_critn_tmp.index("corr")
                            # default params
                            thr = None
                            perc = 0
                            ref = "median"
                            crop_sz = 10  # units of FWHM
                            dist = "pearson"
                            mode = "annulus"
                            inradius = 10
                            width = 20
                            # update if provided
                            if "perc" in badfr_crit_tmp[idx_corr].keys():
                                perc = max(perc, badfr_crit_tmp[idx_corr]["perc"])
                            if "thr" in badfr_crit_tmp[idx_corr].keys():
                                thr = badfr_crit_tmp[idx_corr]["thr"]
                            if "ref" in badfr_crit_tmp[idx_corr].keys():
                                ref = badfr_crit_tmp[idx_corr]["ref"]
                            if ref == "median":
                                good_frame = np.median(cube[zz][final_good_index_list], axis=0)
                            else:
                                good_frame = cube[zz, badfr_crit_tmp[idx_corr]["ref"]]
                            if "crop_sz" in badfr_crit_tmp[idx_corr].keys():
                                crop_sz = badfr_crit_tmp[idx_corr]["crop_sz"]
                            crop_size = int(crop_sz*fwhm[zz])
                            if not crop_size % 2:
                                crop_size += 1
                            if "dist" in badfr_crit_tmp[idx_corr].keys():
                                dist = badfr_crit_tmp[idx_corr]["dist"]
                            if "mode" in badfr_crit_tmp[idx_corr].keys():
                                mode = badfr_crit_tmp[idx_corr]["mode"]
                            if "inradius" in badfr_crit_tmp[idx_corr].keys():
                                inradius = badfr_crit_tmp[idx_corr]["inradius"]
                            if "width" in badfr_crit_tmp[idx_corr].keys():
                                width = badfr_crit_tmp[idx_corr]["width"]

                            crop_size = min(cube[zz].shape[1]-2, crop_size)
                            good_index_list, bad_index_list = cube_detect_badfr_correlation(cube[zz], good_frame,
                                                                                            crop_size=crop_size,
                                                                                            threshold=thr, dist=dist,
                                                                                            percentile=perc, mode=mode,
                                                                                            inradius=inradius, width=width,
                                                                                            plot=plot_tmp, verbose=debug)
                            if plot_tmp:
                                plt.savefig(outpath+f"badfr_corr_plot{labels[fi]}_ch{zz}.pdf", bbox_inches="tight")
                            final_good_index_list = [idx for idx in list(good_index_list) if idx in final_good_index_list]

                        print(f"At the end of channel {zz+1}, we kept {len(final_good_index_list)}/{ngood_fr_ch} ({100*(len(final_good_index_list)/ngood_fr_ch):.0f}%) frames\n", flush=True)

                    if "corr" in badfr_critn_tmp and debug:  # save plots of all reference frames in the case of correlation
                        reference_frames = cube_crop_frames(np.median(cube, axis=1), size=crop_size, verbose=debug)
                        log = False
                        if not coro or fi == 1:  # if no coronagraph the image needs to be log scale
                            log = True
                        plot_frames(tuple(reference_frames), rows=8, cmap="inferno", dpi=300, log=log,
                                    label=tuple(["Channel " + str(x) for x in range(1, 40)]),
                                    save=outpath+f"badfr_corr_all_ref_frames{labels[fi]}.pdf")
                        plt.close("all")
                        del reference_frames

                    cube = cube[:, final_good_index_list]
                    write_fits(outpath+f"2_master{labels[fi]}_ASDIcube_clean_{bad_str}.fits", cube, verbose=debug)
                    if fi != 1:
                        derot_angles = derot_angles[final_good_index_list]
                        write_fits(outpath+f"2_master_derot_angles_clean_{bad_str}.fits", derot_angles, verbose=debug)

                # print final percentage of frames kept
                frac_good = len(final_good_index_list)/nfr
                print("In total we keep {:.1f}% of all frames of the {} cube \n".format(100*frac_good, labels[fi]), flush=True)

            if save_space:
                system(f"rm {outpath}*1bpcorr.fits")

        #************************* FINAL PSF + FLUX + FWHM ************************
        if 5 in to_do and (not isfile(outpath+final_fwhmname+".fits") or overwrite[4]):
            print('************* 5. FINAL PSF + FLUX + FWHM *************', flush=True)
            # use the PSF frames if available, otherwise use non-coro OBJ frames
            if npsf > 0:
                label_step5 = "_psf"
                cube = open_fits(outpath + "2_master_psf_ASDIcube_clean_{}.fits".format("-".join(badfr_criteria_psf)),
                                 verbose=debug)
            elif not npsf and not coro:
                label_step5 = "_obj"
                cube = open_fits(outpath + "2_master_ASDIcube_clean_{}.fits".format("-".join(badfr_criteria)),
                                 verbose=debug)
                print("\nUsing the non-coronagraphic OBJ cubes for PSF + FLUX + FWHM\n", flush=True)
            else:
                raise FileNotFoundError("No suitable frames for PSF + FLUX + FWHM")

            if isinstance(final_crop_szs[1], (float, int)):
                crop_sz_list = [int(final_crop_szs[1])]
            elif isinstance(final_crop_szs[1], list):
                crop_sz_list = final_crop_szs[1]
            else:
                raise TypeError("final_crop_sz_psf should be either int or list of int")

            for crop_sz in crop_sz_list:
                # crop
                if cube.shape[-2] > crop_sz or cube.shape[-1] > crop_sz:
                    if crop_sz % 2 != cube.shape[-1] % 2:
                        for zz in range(cube.shape[0]):
                            cube[zz] = cube_shift(cube[zz], shift_y=0.5, shift_x=0.5, nproc=nproc, imlib=imlib)
                        cube = cube[:, :, 1:, 1:]
                    cube = cube_crop_frames(cube, size=crop_sz, verbose=debug)

                med_psf = np.median(cube, axis=1)
                norm_psf = np.zeros_like(med_psf)
                fwhm = np.zeros(n_z)
                med_flux = np.zeros(n_z)
                for zz in range(n_z):
                    norm_psf[zz], med_flux[zz], fwhm[zz] = normalize_psf(med_psf[zz], fwhm='fit', size=None,
                                                                         threshold=None, mask_core=None,
                                                                         model=psf_model, imlib='opencv',
                                                                         interpolation='lanczos4',
                                                                         force_odd=False, full_output=True,
                                                                         verbose=debug, debug=False)

                write_fits(outpath+"3_final{}_med_{}{:.0f}.fits".format(label_step5, psf_model, crop_sz), med_psf, verbose=debug)
                write_fits(outpath+"3_final{}_med_{}_norm{:.0f}.fits".format(label_step5, psf_model, crop_sz), norm_psf, verbose=debug)
                write_fits(outpath+"3_final{}_flux_med_{}{:.0f}.fits".format(label_step5, psf_model, crop_sz), np.array([med_flux]), verbose=debug)
                write_fits(outpath+"3_final{}_fwhm_{}.fits".format(label_step5, psf_model), np.array([fwhm]), verbose=debug)
                if crop_sz%2:
                    write_fits(outpath+final_psfname+".fits", med_psf, verbose=debug)
                    write_fits(outpath+final_psfname_norm+".fits", norm_psf, verbose=debug)
                    header = fits.Header()
                    header['Flux 0'] = 'Flux scaled to coronagraphic DIT'
                    header['Flux 1'] = 'Flux measured in PSF image'
                    header['Flux 2'] = 'Flux measured in PSF image scale to 1s'
                    if npsf == 0:
                        dit_psf_ifs = dit_ifs
                    write_fits(outpath+final_fluxname+".fits", np.array([med_flux*dit_ifs/dit_psf_ifs, med_flux, med_flux/dit_psf_ifs]),
                               header=header, verbose=debug)
                    write_fits(outpath+final_fwhmname+".fits", fwhm, verbose=debug)

                ntot = cube.shape[1]
                fluxes = np.zeros([n_z,ntot])
                for zz in range(n_z):
                    for nn in range(ntot):
                        _, fluxes[zz,nn], _ = normalize_psf(cube[zz,nn], fwhm=fwhm[zz], size=None, threshold=None, mask_core=None,
                                                       model=psf_model, imlib='opencv', interpolation='lanczos4',
                                                       force_odd=False, full_output=True, verbose=debug, debug=False)
                write_fits(outpath+"3_final{}_fluxes.fits".format(label_step5), fluxes, verbose=debug)

            if save_space:
                system("rm {}*2cen.fits".format(outpath))

        #********************* FINAL OBJ CUBE (BIN IF NECESSARY) ******************
        if 6 in to_do:
            print('************* 6. FINAL OBJ CUBE (BIN IF NECESSARY) *************', flush=True)
            if isinstance(final_crop_szs[0], (float,int)):
                crop_sz_list = [int(final_crop_szs[0])]
            elif isinstance(final_crop_szs[0], list):
                crop_sz_list = final_crop_szs[0]
            else:
                raise TypeError("final_crop_sz_psf should be either int or list of int")
            for cc, crop_sz in enumerate(crop_sz_list):
                # OBJ ONLY
                #for bb, bin_fac in enumerate(bin_fac_list):
                # outpath_bin=path+"3_postproc_bin{:.0f}/".format(bin_fac)
                # if not isdir(outpath_bin):
                #     os.system("mkdir "+outpath_bin)
                if not isfile(outpath+final_cubename+".fits") or overwrite[5]:
                    if use_cen_only:
                        fi_tmp = -1
                    else:
                        fi_tmp=0
                    cube_notrim = open_fits(outpath+"1_master_ASDIcube{}.fits".format(labels[fi_tmp]), verbose=debug)
                    cube = open_fits(outpath+"2_master{}_ASDIcube_clean_{}.fits".format(labels[fi_tmp],"-".join(badfr_criteria)), verbose=debug)
                    derot_angles = open_fits(outpath+"2_master_derot_angles_clean_{}.fits".format("-".join(badfr_criteria)), verbose=debug)
                    derot_angles_notrim = open_fits(outpath+"1_master_derot_angles{}.fits".format(labels[fi_tmp]), verbose=debug)

                    if bin_fac != 1 and bin_fac is not None and bin_fac != 0:
                        cube, derot_angles = cube_subsample(cube, bin_fac, mode="median", parallactic=derot_angles,
                                                            verbose=debug)
                        cube_notrim, derot_angles_notrim = cube_subsample(cube_notrim, bin_fac,
                                                                          mode="median",
                                                                          parallactic=derot_angles_notrim,
                                                                          verbose=debug)
                    # crop
                    if cube.shape[-2] > crop_sz or cube.shape[-1] > crop_sz:
                        if crop_sz%2 != cube.shape[-1]%2:
                            for zz in range(cube.shape[0]):
                                cube[zz] = cube_shift(cube[zz],0.5,0.5, nproc=nproc, imlib=imlib)
                                cube_notrim[zz] = cube_shift(cube_notrim[zz],0.5,0.5, nproc=nproc,
                                                             imlib=imlib)
                            #cube = cube_shift(cube,0.5,0.5)
                            cube = cube[:,:,1:,1:]
                            cube_notrim = cube_notrim[:,:,1:,1:]
                        cube = cube_crop_frames(cube,crop_sz,verbose=debug)
                        cube_notrim = cube_crop_frames(cube_notrim,crop_sz,verbose=debug)
                    flux = open_fits(outpath+final_fluxname+".fits", verbose=debug)
                    # only save final with VIP conventions, for use in postproc.
                    cube_norm = np.zeros_like(cube)
                    cube_notrim_norm = np.zeros_like(cube_notrim)
                    for zz in range(cube.shape[0]):
                        cube_norm[zz] = cube[zz]/flux[0,zz]
                        cube_notrim_norm[zz] = cube_notrim[zz]/flux[0,zz]
                    if crop_sz%2:
                        write_fits(outpath+final_cubename+".fits", cube, verbose=debug)
                        write_fits(outpath+final_cubename_norm+".fits", cube_norm, verbose=debug)
                        write_fits(outpath+final_anglename+".fits", derot_angles, verbose=debug)
                        if plot:
                            plt.close("all")
                            plt.plot(derot_angles)  # plot the first one from each cube
                            plt.xlabel('Science cube')
                            plt.ylabel('Derotation angle [deg]')
                            plt.minorticks_on()
                            plt.grid(alpha=0.1)
                            plt.savefig(outpath + 'Derotation_angle_vector.pdf', bbox_inches='tight', pad_inches=0.1)
                            plt.close('all')
                    write_fits(outpath+"3_final_cube_all_bin{:.0f}_{:.0f}.fits".format(bin_fac, crop_sz), cube_notrim, verbose=debug)
                    write_fits(outpath+"3_final_cube_all_bin{:.0f}_{:.0f}_norm.fits".format(bin_fac, crop_sz), cube_notrim_norm, verbose=debug)
                    write_fits(outpath+"3_final_derot_angles_all_bin{:.0f}.fits".format(bin_fac), derot_angles_notrim, verbose=debug)
                    write_fits(outpath+"3_final_cube_bin{:.0f}_{:.0f}.fits".format(bin_fac, crop_sz), cube, verbose=debug)
                    write_fits(outpath+"3_final_cube_bin{:.0f}_{:.0f}_norm.fits".format(bin_fac, crop_sz), cube_norm, verbose=debug)
                    write_fits(outpath+"3_final_derot_angles_bin{:.0f}.fits".format(bin_fac), derot_angles, verbose=debug)

        #********************* FINAL OBJ ADI CUBES and PSF frames (necessary for NEGFC or MAYO) ******************
        if 7 in to_do:
            print('************* 7. FINAL OBJ ADI CUBES and PSF frames *************', flush=True)
            # OBJs
            if isinstance(final_crop_szs[0], (float,int)):
                crop_sz_list = [int(final_crop_szs[0])]
            elif isinstance(final_crop_szs[0], list):
                crop_sz_list = final_crop_szs[0]
            else:
                raise TypeError("final_crop_sz_psf should be either int or list of int")
            outpath_ADIcubes = outpath+"ADI_cubes/"
            if not isdir(outpath_ADIcubes):
                system("mkdir "+outpath_ADIcubes)
            if not isfile(outpath_ADIcubes+"ADI_cube_ch{:.0f}.fits".format(n_z-1)) or overwrite[6]:
                #for bb, bin_fac in enumerate(bin_fac_list):
                for cc, crop_sz in enumerate(crop_sz_list):
                #ASDI_cube = open_fits(outpath+final_cubename)
                    ASDI_cube = open_fits(outpath+"3_final_cube_all_bin{:.0f}_{:.0f}.fits".format(bin_fac, crop_sz), verbose=debug)
                    for ff in range(n_z):
                        write_fits(outpath_ADIcubes+'ADI_cube_ch{:.0f}_bin{:.0f}_{:.0f}.fits'.format(ff,bin_fac, crop_sz), ASDI_cube[ff], verbose=debug)

            if npsf > 0:
                # PSFs
                if isinstance(final_crop_szs[1], (float,int)):
                    crop_sz_list = [int(final_crop_szs[1])]
                elif isinstance(final_crop_szs[1], list):
                    crop_sz_list = final_crop_szs[1]
                else:
                    raise TypeError("final_crop_sz_psf should be either int or list of int")
                outpath_PSFframes = outpath+"PSF_frames/"
                if not isdir(outpath_PSFframes):
                    system("mkdir "+outpath_PSFframes)
                if not isfile(outpath_PSFframes+"PSF_frame_ch{:.0f}.fits".format(n_z-1)) or overwrite[6]:
                    #for bb, bin_fac in enumerate(bin_fac_list):
                    for cc, crop_sz in enumerate(crop_sz_list):
                    #ASDI_cube = open_fits(outpath+final_cubename)
                        PSF_cube = open_fits(outpath+"3_final_psf_med_{}_norm{:.0f}.fits".format(psf_model,crop_sz), verbose=debug)
                        for ff in range(n_z):
                            write_fits(outpath_PSFframes+'PSF_frame_ch{:.0f}_{:.0f}.fits'.format(ff, crop_sz), PSF_cube[ff], verbose=debug)

        if save_space:
            system("rm {}*1bpcorr.fits".format(outpath))
            system("rm {}*2cen.fits".format(outpath))
            system("rm {}*3crop.fits".format(outpath))


        #********************* FINAL SCALE LIST ******************

        if 8 in to_do:
            nfp = 2  # number of free parameters for simplex search
            print("************* 8. FINDING SCALING FACTORS *************", flush=True)
            fluxes = open_fits(outpath+final_fluxname+".fits", verbose=debug)[0]
            fwhm = open_fits(outpath+final_fwhmname+".fits", verbose=debug)
            derot_angles = open_fits(outpath+final_anglename+".fits", verbose=debug)

            n_cubes = len(derot_angles)
            scal_vector = np.ones([n_z])
            flux_fac_vec = np.ones([n_z])
            resc_cube_res_all = []
            for ff in range(n_z-1):
                cube_ff = open_fits(outpath+final_cubename+".fits", verbose=debug)[ff]
                cube_last = open_fits(outpath+final_cubename+".fits", verbose=debug)[-1]
                master_cube = np.array([cube_ff,cube_last])
                if ff == 0:
                    if isinstance(mask_scal,str):
                        mask_scal = open_fits(mask_scal, verbose=debug)
                        ny_m, nx_m = mask_scal.shape
                        _, ny, nx = cube_ff.shape
                        if ny_m>ny:
                            if ny_m%2 != ny%2:
                                mask_scal = frame_shift(mask_scal, 0.5, 0.5, imlib=imlib)
                                mask_scal = mask_scal[1:,1:]
                            mask_scal=frame_crop(mask_scal,ny)
                        elif ny>ny_m:
                            mask_scal_fin = np.zeros_like(cube_ff[0])
                            mask_scal_fin[:ny_m,:nx_m]=mask_scal
                            mask_scal = frame_shift(mask_scal_fin, (ny-ny_m)/2, (nx-nx_m)/2, imlib=imlib)
                    else:
                        mask = np.ones_like(cube_ff[0])
                        if mask_scal[0]:
                            if mask_scal[1]:
                                mask_scal = get_annulus_segments(mask, mask_scal[0]/plsc,
                                                                 mask_scal[1]/plsc, nsegm=1, theta_init=0,
                                                                 mode="mask")
                            else:
                                mask_scal = mask_circle(mask, mask_scal[0]/plsc)
                        if debug:
                            write_fits(outpath+"TMP_mask_scal.fits", mask_scal, verbose=debug)
                master_cube = np.median(master_cube,axis=1)
                lbdas_tmp = [fwhm[ff],fwhm[-1]]
                fluxes_tmp = [fluxes[ff],fluxes[-1]]
                res = find_scal_vector(master_cube, lbdas_tmp, fluxes_tmp,
                                       mask=mask_scal, nfp=nfp, debug=debug)
                scal_vector[ff], flux_fac_vec[ff] = res[0][0], res[1][0]

            master_cube = open_fits(outpath+final_cubename+".fits", verbose=debug)
            fg = first_good_ch
            lg = last_good_ch
            for i in range(n_cubes):
                resc_cube_res_i = []
                resc_cube = master_cube[fg:lg,i].copy()
                for ch, z in enumerate(range(fg,lg)):
                    resc_cube[ch]*=flux_fac_vec[z]
                resc_cube = cube_rescaling(resc_cube, scal_vector[fg:lg])
                resc_cube_res = np.zeros([lg-fg+1,master_cube.shape[-2],master_cube.shape[-1]])
                resc_cube_res[:-1] = resc_cube
                # how many channels to median combine before simple SDI
                for n_med in n_med_sdi:
                    if n_med>1:
                        tmp1 = np.median(resc_cube[:n_med],axis=0)
                        tmp2 = np.median(resc_cube[-n_med:],axis=0)
                    else:
                        tmp1 = resc_cube[0]
                        tmp2 = resc_cube[-1]
                    resc_cube_res_i.append(tmp2-tmp1)
                resc_cube_res_i = np.array(resc_cube_res_i)
                write_fits(outpath+"TMP_resc_cube{:.0f}_res_{:.0f}fp.fits".format(i,nfp), resc_cube_res_i, verbose=debug)
                resc_cube_res[-1] = tmp2-tmp1
                write_fits(outpath+"TMP_resc_cube_{:.0f}fp.fits".format(nfp), resc_cube, verbose=debug)

            # resc_cube_res_all = np.array(resc_cube_res_all)
            # write_fits(outpath+"TMP_resc_cube_res_all_{:.0f}fp.fits".format(nfp), resc_cube_res_all)

            #perform simple SDI
            for nn,n_med in enumerate(n_med_sdi):
                resc_cube_res_all = []
                for i in range(n_cubes):
                    resc_cube_res_all.append(open_fits(outpath+"TMP_resc_cube{:.0f}_res_{:.0f}fp.fits".format(i,nfp), verbose=debug)[nn])
                resc_cube_res_all = np.array(resc_cube_res_all)

                derot_cube = cube_derotate(resc_cube_res_all, derot_angles, nproc=nproc)
                sdi_frame = np.median(derot_cube,axis=0)
                write_fits(outpath+"final_simple_SDI_{:.0f}fp_nmed{:.0f}.fits".format(nfp,n_med),
                           mask_circle(sdi_frame,coro_sz), verbose=debug)
                stim = stim_map(derot_cube)
                inv_stim_map = inverse_stim_map(resc_cube_res_all, derot_angles, nproc=nproc)
                thr = np.percentile(mask_circle(inv_stim_map, coro_sz), q=99.9)
                norm_stim_map = stim/thr
                stim_maps = np.array([mask_circle(stim,coro_sz),
                                      mask_circle(inv_stim_map,coro_sz),
                                      mask_circle(norm_stim_map,coro_sz)])
                write_fits(outpath+"final_simple_SDI_stim_{:.0f}fp_nmed{:.0f}.fits".format(nfp,n_med), stim_maps, verbose=debug)

            print("original scal guess: ",fwhm[-1]/fwhm[:])
            print("original flux fac guess: ",fluxes[-1]/fluxes[:])
            print("final scal result: ",scal_vector)
            print("final flux fac result ({:.0f}): ".format(nfp),flux_fac_vec, flush=True)
            write_fits(outpath+final_scalefac_name, scal_vector, verbose=debug)
            write_fits(outpath+"final_flux_fac.fits", flux_fac_vec, verbose=debug)
    return None
