from sklearn.utils import resample
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import normalize
from sklearn.ensemble import RandomForestRegressor
from timeit import default_timer as timer
from sklearn.utils import resample
from timeit import default_timer as timer

import shap

from stratx.partdep import *
from stratx.support import *

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from numpy import nan

from test_catmerge import stratify_cats

def speed_ModelID():
    "I believe none of this is in the JIT path; repeated runs are same speed"
    np.random.seed(1)

    n = 20_000
    min_samples_leaf = 5
    X,y = load_bulldozer(n=n)

    leaf_deltas, leaf_counts, refcats, ignored = \
        stratify_cats(X,y,colname="ModelID",min_samples_leaf=min_samples_leaf)

    start = timer()
    _, _, merge_ignored = \
        avg_values_at_cat(leaf_deltas, leaf_counts, refcats, max_iter=10)
    stop = timer()

    nunique = len(np.unique(X['ModelID']))
    print(f"n={n}, unique cats {nunique}, min_samples_leaf={min_samples_leaf}, merge_ignored={merge_ignored}: avg_values_at_cat {stop - start:.3f}s")


if __name__ == '__main__':
    speed_ModelID()