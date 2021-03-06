# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/04_predict.ipynb (unless otherwise specified).

__all__ = ['open_mat', 'crop', 'image2tiles', 'tiles2image', 'get_preds', 'predict_one', 'predict_time',
           'predict_month', 'predict_nrt', 'split_mask']

# Cell
from fastai.vision import *
import scipy.io as sio
import sys
from tqdm import tqdm
import scipy.ndimage as ndimage

from .core import *
from .models import BA_Net

# Cell
def open_mat(fn, slice_idx=None, *args, **kwargs):
    data = sio.loadmat(fn)
    data = np.array([data[r] for r in ['Red', 'NIR', 'MIR', 'FRP']])
    data[np.isnan(data)] = 0
    data[-1, ...] = np.log1p(data[-1,...])
    data[np.isnan(data)] = 0
    if slice_idx is not None:
        return data[:, slice_idx[0]:slice_idx[1], slice_idx[2]:slice_idx[3]]
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

def predict_one(iop:InOutPath, times:list, weights_files:list, region:str, threshold=0.5,
                slice_idx=None):
    fname = lambda t : iop.src/f'VIIRS750{region}_{t.strftime("%Y%m%d")}.mat'
    files = [fname(t) for t in times]
    im_size = open_mat(files[0], slice_idx=slice_idx).shape[1:]
    tiles = []
    print('Loading data and generating tiles:')
    for file in tqdm(files):
        try:
            s = image2tiles(open_mat(file, slice_idx=slice_idx).transpose((1,2,0))).transpose((0,3,1,2))
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

def predict_time(path:InOutPath, times:list, weight_files:list, region,
                 threshold=0.05, save=True, max_size=2000, buffer=128):
    tstart, tend = times.min(), times.max()
    tstart = tstart + pd.Timedelta(days=32)
    tend = tend-pd.Timedelta(days=32)
    tstart = pd.Timestamp(f'{tstart.year}-{tstart.month}-01')
    tend = pd.Timestamp(f'{tend.year}-{tend.month}-01')
    ptimes = pd.date_range(tstart, tend, freq='MS')
    preds_all = []
    si = [[max(0,j*max_size-buffer), (j+1)*max_size+buffer,
           max(0,i*max_size-buffer), (i+1)*max_size+buffer]
          for i in range(region.shape[1]//max_size+1) for j in range(region.shape[0]//max_size+1)]

    bas, bds = [], []
    for i, split in progress_bar(enumerate(si), total=len(si)):
        print(f'Split {split}')
        preds_all = []
        for time in ptimes:
            time_start = pd.Timestamp((time - pd.Timedelta(days=30)).strftime('%Y-%m-15')) # Day 15, previous month
            times = pd.date_range(time_start, periods=64, freq='D')
            preds = predict_one(path, times, weight_files, region.name, slice_idx=split)
            preds = preds[times.month == time.month]
            preds_all.append(preds)
        preds_all = np.concatenate(preds_all, axis=0)
        ba = preds_all.sum(0)
        ba[ba>1] = 1
        ba[ba<threshold] = np.nan
        bd = preds_all.argmax(0)
        bd = bd.astype(float)
        bd[np.isnan(ba)] = np.nan
        #sio.savemat(path.dst/f'data_{i}.mat', {'burndate': bd, 'burnconf': ba}, do_compression=True)
        bas.append(ba)
        bds.append(bd)
    ba_all = np.zeros(region.shape)
    bd_all = np.zeros_like(ba_all)
    for i, split_idx in enumerate(si):
        ba_all[split_idx[0]:split_idx[1], split_idx[2]:split_idx[3]] = bas[i]
        bd_all[split_idx[0]:split_idx[1], split_idx[2]:split_idx[3]] = bds[i]
    if not save: return ba_all, bd_all
    sio.savemat(path.dst/'data.mat', {'burndate': bd_all, 'burnconf': ba_all}, do_compression=True)

def predict_month(iop, time, weight_files, region, threshold=0.5, save=True, slice_idx=None):
    time_start = pd.Timestamp((time - pd.Timedelta(days=30)).strftime('%Y-%m-15')) # Day 15, previous month
    times = pd.date_range(time_start, periods=64, freq='D')
    preds = predict_one(iop, times, weight_files, region, threshold=threshold, slice_idx=slice_idx)
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
    sio.savemat(iop.dst/f'ba_{region}_{tstr}.mat', {'burned': ba, 'date': bd}, do_compression=True)

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
    sio.savemat(iop.dst/f'ba_{tstr}.mat', {'burned': ba, 'date': bd}, do_compression=True)

def split_mask(mask, thr=0.5, thr_obj=1):
    labled, n_objs = ndimage.label(mask > thr)
    result = []
    for i in range(n_objs):
        obj = (labled == i + 1).astype(int)
        if (obj.sum() > thr_obj): result.append(obj)
    return result