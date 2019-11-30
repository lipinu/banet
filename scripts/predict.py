
from fastai.vision import *
import scipy.io as sio
import sys
from tqdm import tqdm
import argparse
import scipy.ndimage as ndimage

from .models import *
from .dataset import InOutPath

__all__ = ['predict_nrt', 'predict_month', 'split_mask']


def open_mat(fn, *args, **kwargs):
    data = sio.loadmat(fn)
    data = np.array([data[r] for r in ['Red', 'NIR', 'MIR', 'FRP']])
    data[np.isnan(data)] = 0
    data[-1, ...] = np.log1p(data[-1,...])
    data[np.isnan(data)] = 0
    return data

def crop(im, r, c, size=128): 
    '''
    crop image into a square of size sz, 
    '''
    sz = size
    out_sz = (sz, sz, im.shape[-1])
    rs,cs,hs = im.shape
    tile = np.zeros(out_sz)
    if (r+sz > rs) and (c+sz > cs):
        tile[:rs-r, :cs-c, :] = im[r:, c:, :]
    elif (r+sz > rs):
        tile[:rs-r, :, :] = im[r:, c:c+sz, :]
    elif (c+sz > cs):
        tile[:, :cs-c, :] = im[r:r+sz ,c:, :]
    else:
        tile[...] = im[r:r+sz, c:c+sz, :]
    return tile


def image2tiles(x, step=100):
    tiles = []
    rr, cc, _ = x.shape
    for c in range(0, cc-1, step):
        for r in range(0, rr-1, step):
            img = crop(x, r, c)
            tiles.append(img)
    return np.array(tiles)
                

def tiles2image(tiles, image_size, size=128, step=100):
    rr, cc, = image_size
    sz = size
    im = np.zeros(image_size)
    indicator = np.zeros_like(im).astype(float)
    k = 0
    for c in range(0, cc-1, step):
        for r in range(0, rr-1, step):
            if (r+sz > rr) and (c+sz > cc):
                im[r:, c:] += tiles[k][:rr-r, :cc-c]
                indicator[r:, c:] += 1
            elif (r+sz > rr):
                im[r:, c:c+sz] += tiles[k][:rr-r, :] 
                indicator[r:, c:c+sz] += 1
            elif (c+sz > cc):
                im[r:r+sz ,c:] += tiles[k][:, :cc-c]
                indicator[r:r+sz, c:] += 1
            else:
                im[r:r+sz, c:c+sz] += tiles[k]
                indicator[r:r+sz, c:c+sz] += 1
            k += 1
    im /= indicator
    return im


def get_preds(tiles, model, weights=None):
    if weights is not None:
        model.load_state_dict(weights)
    mu = tensor([0.2349, 0.3548, 0.1128, 0.0016]).view(1,4,1,1,1)
    std = tensor([0.1879, 0.1660, 0.0547, 0.0776]).view(1,4,1,1,1)
    with torch.no_grad():
        data = []
        for x in tqdm(tiles):
            model.eval().cuda()
            x = (x[None]-mu)/std
            out = model(x.cuda()).sigmoid().float()
            data.append(out.cpu().squeeze().numpy())
    return np.array(data)


def predict_one(iop, times, weights_files, region, threshold=0.5):
    fname = lambda t : iop.inp/f'{region}/VIIRS750{region}_{t.strftime("%Y%m%d")}.mat'
    files = [fname(t) for t in times]
    im_size = open_mat(files[0]).shape[1:]
    tiles = []
    print('Loading data and generating tiles:')
    for file in tqdm(files):
        try:
            s = image2tiles(open_mat(file).transpose((1,2,0))).transpose((0,3,1,2))
        except:
            warn(f'No data for {file}')
            s = np.zeros_like(s)
        tiles.append(s)
    tiles = np.array(tiles).transpose((1, 2, 0, 3, 4))
    tiles = torch.from_numpy(tiles).float()
    preds_ens = []
    for wf in weights_files:
        weights = torch.load(wf)['model']
        print(f'Generating model predictions for {wf}:')
        preds = get_preds(tiles, model=BA_Net(4, 1, 64), weights=weights)
        preds = np.array([tiles2image(preds[:,i], im_size) for i in range(preds.shape[1])])
        preds_ens.append(preds)
    preds = np.array(preds_ens).mean(0)
    return preds


def predict_month(iop, time, weight_files, region, threshold=0.5, save=True):
    time_start = pd.Timestamp((time - pd.Timedelta(days=30)).strftime('%Y-%m-15')) # Day 15, previous month
    times = pd.date_range(time_start, periods=64, freq='D')
    preds = predict_one(iop, times, weight_files, region, threshold=threshold)
    assert preds.shape[0] == len(times)
    preds = preds[times.month == time.month]
    ba = preds.sum(0)
    bd = preds.argmax(0)
    doy = np.asarray(pd.DatetimeIndex(times).dayofyear)
    bd = doy[bd].astype(float)
    bd[bd==doy[0]] = np.nan
    bd[ba<threshold] = np.nan
    ba[ba<threshold] = np.nan
    ba[ba>1] = 1
    if not save: return ba, bd
    tstr = time.strftime('%Y%m')
    sio.savemat(iop.out/f'ba_{region}_{tstr}.mat', {'burned': ba, 'date': bd}, do_compression=True)


def predict_nrt(iop, time, weights_files, region, threshold=0.5, save=True):
    times = pd.date_range(time-pd.Timedelta(days=63), time, freq='D') 
    preds = predict_one(iop, times, weight_files, region, threshold=threshold)
    assert preds.shape[0] == len(times)
    ba = preds.sum(0)
    bd = preds.argmax(0)
    doy = np.asarray(pd.DatetimeIndex(times).dayofyear)
    bd = doy[bd].astype(float)
    bd[bd==doy[0]] = np.nan
    bd[ba<threshold] = np.nan
    ba[ba<threshold] = np.nan
    ba[ba>1] = 1
    if not save: return ba, bd
    tstr = time.strftime('%Y%m%d')
    sio.savemat(iop.out/f'ba_{tstr}.mat', {'burned': ba, 'date': bd}, do_compression=True)


def split_mask(mask, thr=0.5, thr_obj=1):
    labled, n_objs = ndimage.label(mask > thr)
    result = []
    for i in range(n_objs):
        obj = (labled == i + 1).astype(int)
        if (obj.sum() > thr_obj): result.append(obj)
    return result