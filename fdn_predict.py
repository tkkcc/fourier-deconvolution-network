from __future__ import print_function, unicode_literals, absolute_import, division

import numpy as np
import os, sys, json, argparse, datetime
import keras.backend as K

from scipy.signal import fftconvolve
from skimage.io import imread, imsave
from skimage import img_as_float
from pprint import pprint
from model import model_stacked
from skimage.measure import compare_psnr

# https://stackoverflow.com/a/43357954
def str2bool(v):
    if v.lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif v.lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected.")


def is_ipython():
    try:
        __IPYTHON__
        return True
    except NameError:
        return False


def parse_args():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    data = parser.add_argument_group("input")
    data.add_argument("--image", metavar=None, type=str, default=None, help="blurred image")
    data.add_argument("--kernel", metavar=None, type=str, default=None, help="blur kernel")
    data.add_argument(
        "--sigma",
        metavar=None,
        type=float,
        default=1.5,
        help="standard deviation of Gaussian noise",
    )
    data.add_argument(
        "--flip-kernel",
        metavar=None,
        type=str2bool,
        default=True,
        const=True,
        nargs="?",
        help="rotate blur kernel by 180 degrees",
    )

    model = parser.add_argument_group("model")
    model.add_argument(
        "--model-dir", metavar=None, type=str, default="models/sigma_1.0-3.0", help="path to model"
    )
    model.add_argument(
        "--n-stages", metavar=None, type=int, default=10, help="number of model stages to use"
    )
    model.add_argument(
        "--finetuned",
        metavar=None,
        type=str2bool,
        default=True,
        const=True,
        nargs="?",
        help="use finetuned model weights",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--output", metavar=None, type=str, default=None, help="deconvolved result image"
    )
    output.add_argument(
        "--save-all-stages",
        metavar=None,
        type=str2bool,
        default=False,
        const=True,
        nargs="?",
        help="save all intermediate results (if finetuned is false)",
    )

    parser.add_argument(
        "--quiet",
        metavar=None,
        type=str2bool,
        default=False,
        const=True,
        nargs="?",
        help="don't print status messages",
    )

    return parser.parse_args()


def to_tensor(img):
    if img.ndim == 2:
        return img[np.newaxis, ..., np.newaxis]
    elif img.ndim == 3:
        return np.moveaxis(img, 2, 0)[..., np.newaxis]


def from_tensor(img):
    return np.squeeze(np.moveaxis(img[..., 0], 0, -1))


def pad_for_kernel(img, kernel, mode):
    p = [(d - 1) // 2 for d in kernel.shape]
    padding = [p, p] + (img.ndim - 2) * [(0, 0)]
    return np.pad(img, padding, mode)


def crop_for_kernel(img, kernel):
    p = [(d - 1) // 2 for d in kernel.shape]
    r = [slice(p[0], -p[0]), slice(p[1], -p[1])] + (img.ndim - 2) * [slice(None)]
    return img[r]


def edgetaper_alpha(kernel, img_shape):
    v = []
    for i in range(2):
        z = np.fft.fft(np.sum(kernel, 1 - i), img_shape[i] - 1)
        z = np.real(np.fft.ifft(np.square(np.abs(z)))).astype(np.float32)
        z = np.concatenate([z, z[0:1]], 0)
        v.append(1 - z / np.max(z))
    return np.outer(*v)


def edgetaper(img, kernel, n_tapers=3):
    alpha = edgetaper_alpha(kernel, img.shape[0:2])
    _kernel = kernel
    if 3 == img.ndim:
        kernel = kernel[..., np.newaxis]
        alpha = alpha[..., np.newaxis]
    for i in range(n_tapers):
        blurred = fftconvolve(pad_for_kernel(img, _kernel, "wrap"), kernel, mode="valid")
        img = alpha * img + (1 - alpha) * blurred
    return img


def load_json(path, fname="config.json"):
    with open(os.path.join(path, fname), "r") as f:
        return json.load(f)


def save_result(result, path):
    path = path if path.find(".") != -1 else path + ".png"
    ext = os.path.splitext(path)[-1]
    if ext in (".txt", ".dlm"):
        np.savetxt(path, result)
    else:
        imsave(path, np.clip(result, 0, 1))


def show(x, title=None, cbar=False, figsize=None):
    import matplotlib.pyplot as plt

    plt.figure(figsize=figsize)
    plt.imshow(x, interpolation="nearest", cmap="gray")
    if title:
        plt.title(title)
    if cbar:
        plt.colorbar()
    plt.show()


if __name__ == "__main__":

    # parse arguments & setup
    args = parse_args()
    if args.quiet:
        log = lambda *args, **kwargs: None
    else:

        def log(*args, **kwargs):
            print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S:"), *args, **kwargs)

    if not args.quiet:
        log("Arguments:")
        pprint(vars(args))
    if args.output is None:
        import matplotlib.pyplot as plt

        if is_ipython():
            plt.ion()

    # load model config and do some sanity checks
    config = load_json(args.model_dir)
    n_stages = config["n_stages"] if args.n_stages is None else args.n_stages
    assert config["sigma_range"][0] <= args.sigma <= config["sigma_range"][1]
    assert 0 < n_stages <= config["n_stages"]

    # load inputs
    # img = img_as_float(imread(args.image)).astype(np.float32)
    # if args.kernel.find('.') != -1 and os.path.splitext(args.kernel)[-1].startswith('.tif'):
    #     kernel = imread(args.kernel).astype(np.float32)
    # else:
    #     kernel = np.loadtxt(args.kernel).astype(np.float32)
    # load models
    # os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
    K.clear_session()
    log("Processing stages 01-%02d" % n_stages)
    log("- creating models and loading weights")
    weights = os.path.join(
        args.model_dir,
        "stages_01-%02d_%s.hdf5" % (n_stages, "finetuned" if args.finetuned else "greedy"),
    )
    psnrs = []
    if os.path.exists(weights):
        m = model_stacked(n_stages)
        m.load_weights(weights)
    else:
        assert not args.finetuned
        weights = [
            os.path.join(args.model_dir, "stage_%02d.hdf5" % (t + 1)) for t in range(n_stages)
        ]
        m = model_stacked(n_stages, weights)
    from scipy.io import loadmat
    from pathlib import Path

    for i in Path("data/Levin09blurdata").iterdir():
        print(i)
        mat = loadmat(i)
        gt = mat["x"].astype(np.float32)
        img = mat["y"].astype(np.float32)
        kernel = mat["f"].astype(np.float32)
        assert gt.shape == img.shape
        # gt.clip(0,1)
        # gt = crop_for_kernel(gt,kernel)
        # img = crop_for_kernel(img,kernel)
        # gt = crop_for_kernel(gt,kernel)
        # img = crop_for_kernel(img,kernel)
        # gt = crop_for_kernel(gt,kernel)
        # img = crop_for_kernel(img,kernel)
        # gt = crop_for_kernel(gt,kernel)
        # img = crop_for_kernel(img,kernel)
        # img = (img - img.min()) / (img.max() - img.min())
        # gt = (gt - gt.min()) / (gt.max() - gt.min())
        # show(gt,'gt')
        # show(img,'blured')
        # show(kernel,'k')
        # args.flip_kernel = True
        if args.flip_kernel:
            kernel = kernel[::-1, ::-1]
        kernel = np.clip(kernel, 0, 1)
        kernel /= np.sum(kernel)
        assert 2 <= img.ndim <= 3
        assert kernel.ndim == 2 and all([d % 2 == 1 for d in kernel.shape])
        # prepare for prediction
        y = to_tensor(edgetaper(pad_for_kernel(img, kernel, "edge"), kernel))
        k = np.tile(kernel[np.newaxis], (y.shape[0], 1, 1))
        s = np.tile(args.sigma, (y.shape[0], 1)).astype(np.float32)
        x0 = y

        # predict
        pred = m.predict_on_batch([x0, y, k, s])
        if n_stages == 1:
            pred = [pred]

        # save or show
        # result = crop_for_kernel(from_tensor(pred[n_stages - 1]), kernel)
        a = from_tensor(pred[n_stages - 1])
        gt = crop_for_kernel(gt, kernel)
        img = crop_for_kernel(img, kernel)
        hh, ww = a.shape
        h, w = gt.shape
        psnr = result = top = left = 0
        for t in range(hh - h):
            for l in range(ww - w):
                r = a[t : t + h, l : l + w]
                p = compare_psnr(gt, r)
                if p > psnr:
                    left = l
                    top = t
                    psnr = p
                    result = r

        # result = result.clip(0, 1)
        # gt = gt.clip(0, 1)
        # result /= result.sum()
        # gt /= gt.sum()
        # result = (result - result.min()) / (result.max() - result.min())
        # gt = (gt - gt.min()) / (gt.max() - gt.min())
        # gt = crop_for_kernel(gt, kernel)
        # img = crop_for_kernel(img, kernel)
        # result = crop_for_kernel(result, kernel)
        # psnr = compare_psnr(gt, result)
        # psnr = (gt - result) ** 2
        # psnr = np.mean(psnr)
        # psnr = -10* np.log10(psnr)
        psnrs.append(psnr)
        print(f"{psnr:.2f}", top, left, hh,ww, h,w, *kernel.shape)
        # show(np.concatenate((img, gt, result), 1))
    print("psnr avg:", f"{np.mean(psnrs):.2f}")
    print("psnr max:", f"{np.max(psnrs):.2f}")

