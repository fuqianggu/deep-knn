import os
import pickle
import numpy as np
import itertools
import argparse
from tqdm import tqdm

import cupy
import chainer
from chainer import functions as F

from nlp_utils import convert_seq, convert_snli_seq
from utils import setup_model


def get_onehot_grad(model, xs, ys=None):
    if ys is None:
        with chainer.using_config('train', False):
            ys = model.predict(xs, argmax=True)
            ys = F.expand_dims(ys, axis=1)
            ys = [y for y in ys]
    loss, exs = model.get_grad(xs, ys)
    exs_grad = chainer.grad([loss], exs)
    ex_sections = np.cumsum([ex.shape[0] for ex in exs[:-1]])
    exs = F.concat(exs, axis=0)
    exs_grad = F.concat(exs_grad, axis=0)
    onehot_grad = F.sum(exs_grad * exs, axis=1)
    onehot_grad = F.split_axis(onehot_grad, ex_sections, axis=0)
    return onehot_grad


def remove_one(model, xs, n_beams, indices, removed_indices, max_beam_size=5):
    n_examples = len(n_beams)
    onehot_grad = [x.data for x in get_onehot_grad(model, xs)]
    xp = cupy.get_array_module(*onehot_grad)
    # don't remove <eos>
    order = [xp.argsort(x[:-1]).tolist() for x in onehot_grad]

    new_xs = []
    new_n_beams = []
    new_indices = []
    new_removed_indices = []

    start = 0
    for example_idx in range(n_examples):
        if n_beams[example_idx] == 0:
            new_n_beams.append(0)
            continue

        coordinates = []
        for i in range(start, start + n_beams[example_idx]):
            for j in order[i][:max_beam_size]:
                coordinates.append((onehot_grad[i][j], (i, j)))
        if len(coordinates) == 0:
            new_n_beams.append(0)
            start += n_beams[example_idx]
            continue

        coordinates = sorted(coordinates, key=lambda x: -x[0])
        coordinates = [c for _, c in coordinates][:max_beam_size]
        for i, j in coordinates:
            x = xs[i].copy()
            x = xp.concatenate([x[:j], x[j+1:]], axis=0)
            new_xs.append(x)
            new_indices.append(indices[i][:j] + indices[i][j+1:])
            new_removed_indices.append(removed_indices[i] + [indices[i][j]])
        new_n_beams.append(len(coordinates))
        start += n_beams[example_idx]
    return new_xs, new_n_beams, new_indices, new_removed_indices


def get_rawr(model, xs, max_beam_size=5):
    n_examples = len(xs)
    n_beams = [1 for _ in xs]
    indices = [list(range(x.shape[0])) for x in xs]
    removed_indices = [[] for _ in xs]

    final_xs = [[x.tolist()] for x in xs]
    final_removed = [[[]] for _ in xs]
    final_length = [x.shape[0] for x in xs]

    with chainer.using_config('train', False):
        ys_0 = model.predict(xs, argmax=True)

    while True:
        xs, n_beams, indices, removed_indices = remove_one(
                model, xs, n_beams, indices, removed_indices, max_beam_size)
        with chainer.using_config('train', False):
            ys = model.predict(xs, argmax=True)

        new_n_beams = []
        start = 0
        remained_indices = []
        for example_idx in range(n_examples):
            if n_beams[example_idx] == 0:
                new_n_beams.append(0)
                continue

            cnt = 0
            for i in range(start, start + n_beams[example_idx]):
                if not ys[i] == ys_0[example_idx]:
                    continue

                x = xs[i].tolist()
                if len(x) < final_length[example_idx]:
                    final_length[example_idx] = len(x)
                    final_removed[example_idx] = [removed_indices[i]]
                    final_xs[example_idx] = [x]
                elif len(x) == final_length[example_idx]:
                    if x not in final_xs[example_idx]:
                        final_xs[example_idx].append(x)
                        final_removed[example_idx].append(removed_indices[i])
                if len(x) == 1:
                    # only eos left
                    continue

                remained_indices.append(i)
                cnt += 1
            new_n_beams.append(cnt)
            start += n_beams[example_idx]

        if len(remained_indices) == 0:
            break

        xs = [xs[i] for i in remained_indices]
        indices = [indices[i] for i in remained_indices]
        removed_indices = [removed_indices[i] for i in remained_indices]
        n_beams = new_n_beams
    return final_xs, final_removed


class Bunch(object):

    def __init__(self, adict):
        self.__dict__.update(adict)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', '-g', type=int, default=0,
                        help='GPU ID (negative value indicates CPU)')
    parser.add_argument('--model-setup', required=True,
                        help='Model setup dictionary.')
    parser.add_argument('--lsh', action='store_true', default=False,
                        help='If true, uses locally sensitive hashing \
                              (with k=10 NN) for NN search.')
    args = parser.parse_args()

    model, train, test, vocab, setup = setup_model(args)
    if setup['dataset'] == 'snli' and not setup['combine_snli']:
        converter = convert_snli_seq
    else:
        converter = convert_seq

    # FIXME
    args.batchsize = 64
    max_beam_size = 5

    test_iter = chainer.iterators.SerialIterator(
            test, args.batchsize, repeat=False, shuffle=False)

    checkpoint = []
    for batch_idx, batch in enumerate(tqdm(test_iter)):
        if batch_idx > 10:
            break

        batch = converter(batch, device=args.gpu)
        xs = batch['xs']
        reduced_xs, removed_indices = get_rawr(
                model, xs, max_beam_size=max_beam_size)

        xp = cupy.get_array_module(*xs)
        n_finals = [len(r) for r in reduced_xs]
        reduced_xs = list(itertools.chain(*reduced_xs))
        removed_indices = list(itertools.chain(*removed_indices))
        reduced_xs = [xp.asarray(x) for x in reduced_xs]
        reduced_xs = converter(reduced_xs, device=args.gpu, with_label=False)
        with chainer.using_config('train', False):
            ss_0 = xp.asnumpy(model.predict(xs, softmax=True))
            ss_1 = xp.asnumpy(model.predict(reduced_xs, softmax=True))
            ys_0 = np.argmax(ss_0, axis=1)
            ys_1 = np.argmax(ss_1, axis=1)

        start = 0
        for example_idx in range(len(xs)):
            oi = xs[example_idx].tolist()  # original input
            op = int(ys_0[example_idx])  # original predictoin
            oos = ss_0[example_idx]  # original output distribution
            label = int(batch['ys'][example_idx])
            checkpoint.append([])
            for i in range(start, start + n_finals[example_idx]):
                ri = reduced_xs[i].tolist()
                rp = int(ys_1[i])
                rs = ss_1[i]
                rr = removed_indices[i]
                entry = {'original_input': oi,
                         'reduced_input': ri,
                         'original_prediction': op,
                         'reduced_prediction': rp,
                         'original_scores': oos,
                         'reduced_scores': rs,
                         'removed_indices': rr,
                         'label': label}
                checkpoint[-1].append(entry)
    with open(os.path.join('rawr_dev.pkl'), 'wb') as f:
        pickle.dump(checkpoint, f)


if __name__ == '__main__':
    main()
