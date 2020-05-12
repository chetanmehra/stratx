"""
MIT License

Copyright (c) 2019 Terence Parr

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib as mpl
from  matplotlib.collections import LineCollection
from sklearn.linear_model import LinearRegression
from sklearn.ensemble import RandomForestRegressor, RandomForestClassifier
import warnings
from sklearn.utils import resample
from collections import defaultdict
from typing import Sequence

from numba import jit, prange

import stratx.featimp as featimp


def leaf_samples(rf, X_not_col:np.ndarray) -> Sequence:
    """
    Return a list of arrays where each array is the set of X sample indexes
    residing in a single leaf of some tree in rf forest. For example, if there
    are 4 leaves (in one or multiple trees), we might return:

        array([array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]),
           array([10, 11, 12, 13, 14, 15]), array([16, 17, 18, 19, 20]),
           array([21, 22, 23, 24, 25, 26, 27, 28, 29]))
    """
    n_trees = len(rf.estimators_)
    leaf_samples = []
    leaf_ids = rf.apply(X_not_col)  # which leaf does each X_i go to for sole tree?
    for t in range(n_trees):
        # Group by id and return sample indexes
        uniq_ids = np.unique(leaf_ids[:,t])
        sample_idxs_in_leaves = [np.where(leaf_ids[:, t] == id)[0] for id in uniq_ids]
        leaf_samples.extend(sample_idxs_in_leaves)
    return leaf_samples


def partial_dependence(X:pd.DataFrame, y:pd.Series, colname:str,
                       min_slopes_per_x=5,
                       parallel_jit=True,
                       n_trees=1, min_samples_leaf=15, rf_bootstrap=False, max_features=1.0,
                       supervised=True,
                       verbose=False):
    """
    Internal computation of partial dependence information about X[colname]'s effect on y.
    Also computes partial derivative of y with respect to X[colname].

    :param X: Dataframe with all explanatory variables
    :param y: Series or vector with response variable
    :param colname: which X[colname] (a string) to compute partial dependence for
    :param min_slopes_per_x: ignore pdp y values derived from too few slopes; this is
           same count across all features (tried percentage of max slope count but was
           too variable). Important for getting good starting point of PD.

    Returns:
        leaf_xranges    The ranges of X[colname] partitions


        leaf_slopes     Associated slope for each leaf xrange

        slope_counts_at_x How many slopes are available at each x_j location

        dx              The change in x from one non-NaN unique X[colname] to the next

        slope_at_x      The slope at each non-NaN unique X[colname]

        pdpx            The non-NaN unique X[colname] values; len(pdpx)<=len(unique(X[colname]))

        pdpy            The effect of each non-NaN unique X[colname] on y; effectively
                        the cumulative sum (integration from X[colname] x to z for all
                        z in X[colname]). The first value is always 0.

        ignored         How many samples from len(X) total records did we have to
                        ignore because samples in leaves had identical X[colname]
                        values.
  """
    X_not_col = X.drop(colname, axis=1).values
    # For x floating-point numbers that are very close, I noticed that np.unique(x)
    # was treating floating-point numbers different in the 12th decimal point as different.
    # This caused a number of problems likely but I didn't notice it until I tried
    # np.gradient(), which found extremely huge derivatives. I fixed that with a hack:
    X_col = X[colname].values.round(decimals=10)

    if supervised:
        rf = RandomForestRegressor(n_estimators=n_trees,
                                   min_samples_leaf=min_samples_leaf,
                                   bootstrap=rf_bootstrap,
                                   max_features=max_features)
        rf.fit(X_not_col, y)
        if verbose:
            print(f"Strat Partition RF: dropping {colname} training R^2 {rf.score(X_not_col, y):.2f}")

    else:
        """
        Wow. Breiman's trick works in most cases. Falls apart on Boston housing MEDV target vs AGE
        """
        if verbose: print("USING UNSUPERVISED MODE")
        X_synth, y_synth = conjure_twoclass(X)
        rf = RandomForestClassifier(n_estimators=n_trees,
                                    min_samples_leaf=min_samples_leaf,
                                    bootstrap=rf_bootstrap,
                                    max_features=max_features,
                                    oob_score=False)
        rf.fit(X_synth.drop(colname, axis=1), y_synth)

    if verbose:
        leaves = leaf_samples(rf, X_not_col)
        nnodes = rf.estimators_[0].tree_.node_count
        print(f"Partitioning 'x not {colname}': {nnodes} nodes in (first) tree, "
              f"{len(rf.estimators_)} trees, {len(leaves)} total leaves")

    leaf_xranges, leaf_slopes, ignored = \
        collect_discrete_slopes(rf, X_col, X_not_col, y) # if ignored, won't have entries in leaf_* results

    # print('leaf_xranges', leaf_xranges)
    # print('leaf_slopes', leaf_slopes)

    real_uniq_x = np.unique(X_col)   # comes back sorted
    if verbose:
        print(f"discrete StratPD num samples ignored {ignored}/{len(X)} for {colname}")

    #print("uniq x =", len(real_uniq_x), "slopes.shape =", leaf_slopes.shape, "x ranges.shape", leaf_xranges.shape)
    if parallel_jit:
        slope_at_x, slope_counts_at_x = \
            avg_slopes_at_x_jit(real_uniq_x, leaf_xranges, leaf_slopes)
    else:
        slope_at_x, slope_counts_at_x = \
            avg_slopes_at_x_nonparallel_jit(real_uniq_x, leaf_xranges, leaf_slopes)

    if min_slopes_per_x <= 0:
        min_slopes_per_x = 1 # must have at least one slope value

    # Turn any slopes with weak evidence into NaNs but keep same slope_at_x length
    slope_at_x = np.where(slope_counts_at_x >= min_slopes_per_x, slope_at_x, np.nan)

    pdpx = real_uniq_x

    # At this point, slope_at_x will have at least one nan, but that's okay because
    # diff and nancumsum will treat them appropriately.  nancumsum treats NaN as 0
    # so the previous non-NaN value is carried forward until we reach next real value.
    # Keep in mind that the last slope in slope_at_x is always nan,
    # since we have no information beyond that. However, we can still produce
    # a pdpy value for that last position because we have a delta from the
    # previous position that will get us to the last position.

    # Integrate the partial derivative estimate in slope_at_x across pdpx to get dependence
    dx = np.diff(pdpx)      # for n pdpx values, there are n-1 dx values
    dydx = slope_at_x[:-1]  # for n y values, only n-1 slope values; last slope always nan

    y_deltas = dydx * dx                          # get change in y from x[i] to x[i+1]
    pdpy = np.nancumsum(y_deltas)                 # y_deltas has one less value than num x values
    pdpy = np.concatenate([np.array([0]), pdpy])  # align with x values; our PDP y always starts from zero

    # At this point pdpx, pdpy have the same length as real_uniq_x

    # Strip from pdpx,pdpy any positions for which we don't have useful slope info. If we have
    # slopes = [1, 3, nan] then cumsum will give 3 valid pdpy values. But, if we have
    # slopes = [1, 3, nan, nan], then there is no pdpy value for last position. nancumsum
    # carries same y to the right, but we wanna strip last position, in this case.
    # Detect 2 nans in a row. We don't want the second or any after that until next real value
    idx_not_adjacent_nans = np.where(~(np.isnan(slope_at_x[1:]) & np.isnan(slope_at_x[:-1])))[0] + 1
    # Deal with first position. If it's nan, drop it, else keep it
    if not np.isnan(slope_at_x[0]):
        idx_not_adjacent_nans = np.concatenate([np.array([0]), idx_not_adjacent_nans])
    pdpx = pdpx[idx_not_adjacent_nans]
    pdpy = pdpy[idx_not_adjacent_nans]

    return leaf_xranges, leaf_slopes, slope_counts_at_x, dx, slope_at_x, pdpx, pdpy, ignored


def plot_stratpd(X:pd.DataFrame, y:pd.Series, colname:str, targetname:str,
                 min_slopes_per_x=5,
                 n_trials=1,
                 n_trees=1,
                 min_samples_leaf=15,
                 bootstrap=True,
                 subsample_size=.75,
                 rf_bootstrap=False,
                 max_features=1.0,
                 supervised=True,
                 ax=None,
                 xrange=None,
                 yrange=None,
                 title=None,
                 show_xlabel=True,
                 show_ylabel=True,
                 show_pdp_line=False,
                 show_all_pdp=True,
                 show_slope_lines=False,
                 show_slope_counts=False,
                 show_x_counts=True,
                 show_impact=False,
                 show_impact_dots=True,
                 show_impact_line=True,
                 hide_top_right_axes=True,
                 pdp_marker_size=2,
                 pdp_marker_alpha=.5,
                 pdp_line_width=.5,
                 slope_line_color='#2c7fb8',
                 slope_line_width=.5,
                 slope_line_alpha=.3,
                 pdp_line_color='black',
                 pdp_marker_color='black',
                 pdp_marker_cmap='coolwarm',
                 impact_fill_color='#FEF5DC',
                 impact_pdp_color='#D73028',
                 impact_marker_size=3,
                 fontname='Arial',
                 title_fontsize=11,
                 label_fontsize=10,
                 ticklabel_fontsize=10,
                 barchart_size=0.20,
                 barchar_alpha=1.0, # if show_slope_counts, what ratio of vertical space should barchart use at bottom?
                 barchar_color='#BABABA',
                 verbose=False,
                 figsize=None
                 ):
    """
    Plot the partial dependence of X[colname] on y for numerical X[colname].

    Key parameters:

    :param X: Dataframe with all explanatory variables

    :param y: Series or vector with response variable

    :param colname: which X[colname] (a string) to compute partial dependence for

    :param targetname: for plotting purposes, will what is the y axis label?

    :param n_trials:  How many times should we run the stratpd algorithm and get PD
                      curves, using bootstrapped or subsample sets? Default is 1.

    :param min_samples_leaf Key hyper parameter to the stratification
                            process. The default is 15 and usually
                            works out pretty well.  It controls the
                            minimum number of observations in each
                            decision tree leaf used to stratify X other than colname.
                            Generally speaking,
                            smaller values lead to more confidence
                            that fluctuations in y are due solely to
                            X[colname], but more observations per leaf allow
                            StratPD to capture more nonlinearities and
                            make it less susceptible to noise. As the
                            leaf size grows, however, one risks
                            introducing contributions from X not colname into
                            the relationship between X[colname] and y. At the
                            extreme, the decision tree would consist
                            of a single leaf node containing all
                            observations, leading to a marginal not
                            partial dependence curve.

    :param min_slopes_per_x: ignore any partial derivatives estimated
                             with too few observations. Dropping uncertain partial derivatives
                             greatly improves accuracy and stability. Partial dependences
                             computed by integrating over local partial derivatives are highly
                             sensitive to partial derivatives computed at the left edge of any
                             X[colname]’s range because imprecision at the left edge affects the entire
                             curve. This presents a problem when there are few samples with X[colname]
                             values at the extreme left. Default is 5.

    Returns:

        pdpx            The non-NaN unique X[colname] values

        pdpy            The effect of each non-NaN unique X[colname] on y; effectively
                        the cumulative sum of the partial derivative of y with respect to
                        X[colname]. The first value is always 0.

        ignored         How many samples from len(X) total records did we have to
                        ignore because samples in leaves had identical X[colname]
                        values.
    """
    def avg_pd_curve(all_pdpx, all_pdpy):
        m = defaultdict(float)
        c = defaultdict(int)
        for i in range(n_trials):
            for px, py in zip(all_pdpx, all_pdpy):
                for x, y in zip(px, py):
                    m[x] += y
                    c[x] += 1
        for x in m.keys():
            m[x] /= c[x]

        # We now have dict with average pdpy for each pdpx found in any curve
        # but we need to ensure we get it back in pdpx order
        pdpx = np.array(sorted(m.keys()))
        pdpy = np.empty(shape=(len(m),))
        for i,x in enumerate(pdpx):
            pdpy[i] = m[x]
        return pdpx, pdpy

    X_col = X[colname].values.round(decimals=10)

    all_pdpx = []
    all_pdpy = []
    impacts = []
    importances = []
    n = len(X)
    ignored = 0
    for i in range(n_trials):
        if n_trials>1:
            if bootstrap:
                idxs = resample(range(n), n_samples=n, replace=True) # bootstrap
            else: # subsample
                idxs = resample(range(n), n_samples=int(n*subsample_size), replace=False)
            X_, y_ = X.iloc[idxs], y.iloc[idxs]
        else:
            X_, y_ = X, y

        leaf_xranges, leaf_slopes, slope_counts_at_x, dx, slope_at_x, pdpx, pdpy, ignored_ = \
            partial_dependence(X=X_, y=y_, colname=colname,
                               min_slopes_per_x=min_slopes_per_x,
                               n_trees=n_trees, min_samples_leaf=min_samples_leaf,
                               rf_bootstrap=rf_bootstrap, max_features=max_features,
                               supervised=supervised,
                               verbose=verbose)
        ignored += ignored_
        # print("ignored", ignored_, "pdpy", pdpy)
        all_pdpx.append(pdpx)
        all_pdpy.append(pdpy)
        impact, importance = featimp.compute_importance(X_col, pdpx, pdpy)
        impacts.append(impact)
        importances.append(importance)

    ignored /= n_trials # average number of x values ignored across trials

    if ax is None:
        if figsize is not None:
            fig, ax = plt.subplots(1, 1, figsize=figsize)
        else:
            fig, ax = plt.subplots(1, 1)

    avg_pdp_marker_size = pdp_marker_size
    if show_all_pdp and n_trials>1:
        sorted_by_imp = np.argsort([np.mean(np.abs(v)) for v in all_pdpy])
        cmap = plt.get_cmap(pdp_marker_cmap)
        ax.set_prop_cycle(color=cmap(np.linspace(0,1,num=n_trials)))
        for i in range(n_trials):
            ax.plot(all_pdpx[sorted_by_imp[i]], all_pdpy[sorted_by_imp[i]],
                    '.', markersize=pdp_marker_size, alpha=pdp_marker_alpha)
        avg_pdp_marker_size += 2

    # Get avg curve, reset pdpx and pdpy to the average
    pdpx, pdpy = avg_pd_curve(all_pdpx, all_pdpy)
    ax.plot(pdpx, pdpy, '.', c=pdp_marker_color, markersize=avg_pdp_marker_size, label=colname)

    if show_pdp_line:
        ax.plot(pdpx, pdpy, lw=pdp_line_width, c=pdp_line_color)

    if len(pdpy)==0:
        raise ValueError("No partial dependence y values, often due to value of min_samples_leaf that is too small or min_slopes_per_x that is too large")

    min_y = min(pdpy)
    max_y = max(pdpy)
    if n_trials==1 and show_slope_lines:
        segments = []
        for xr, slope in zip(leaf_xranges, leaf_slopes):
            w = np.abs(xr[1] - xr[0])
            delta_y = slope * w
            closest_x_i = np.abs(pdpx - xr[0]).argmin() # find curve point for xr[0]
            closest_x = pdpx[closest_x_i]
            closest_y = pdpy[closest_x_i]
            slope_line_endpoint_y = closest_y + delta_y
            one_line = [(closest_x, closest_y), (closest_x + w, slope_line_endpoint_y)]
            segments.append( one_line )
            if slope_line_endpoint_y < min_y:
                min_y = slope_line_endpoint_y
            elif slope_line_endpoint_y > max_y:
                max_y = slope_line_endpoint_y

        lines = LineCollection(segments, alpha=slope_line_alpha, color=slope_line_color, linewidths=slope_line_width)
        ax.add_collection(lines)

    if xrange is not None:
        ax.set_xlim(*xrange)
    # else:
    #     ax.set_xlim(*domain)
    if yrange is not None:
        ax.set_ylim(*yrange)
    else:
        ax.set_ylim(min_y, max_y)

    leave_room_scaler = 1.3
    x_width = max(pdpx) - min(pdpx) + 1
    count_bar_width = x_width / len(pdpx)
    if count_bar_width/x_width < 0.002:
        count_bar_width = x_width * 0.002 # don't make them so skinny they're invisible
    # print(f"x_width={x_width:.2f}, count_bar_width={count_bar_width}")
    if show_x_counts:
        _, pdpx_counts = np.unique(X_col[np.isin(X_col, pdpx)], return_counts=True)
        ax2 = ax.twinx()
        # scale y axis so the max count height is 10% of overall chart
        ax2.set_ylim(0, max(pdpx_counts) * 1/barchart_size)
        # draw just 0 and max count
        ax2.yaxis.set_major_locator(plt.FixedLocator([0, max(pdpx_counts)]))
        ax2.bar(x=pdpx, height=pdpx_counts, width=count_bar_width,
                facecolor=barchar_color, align='center', alpha=barchar_alpha)
        ax2.set_ylabel(f"PD $x$ point count\nignored={ignored:.0f}", labelpad=-18, fontsize=label_fontsize,
                       fontstretch='extra-condensed',
                       fontname=fontname)
        # shift other y axis down barchart_size to make room
        if yrange is not None:
            ax.set_ylim(yrange[0] - (yrange[1]-yrange[0]) * barchart_size * leave_room_scaler, yrange[1])
        else:
            ax.set_ylim(min_y-(max_y-min_y)*barchart_size * leave_room_scaler, max_y)
        ax2.tick_params(axis='both', which='major', labelsize=ticklabel_fontsize)
        for tick in ax2.get_xticklabels():
            tick.set_fontname(fontname)
        for tick in ax2.get_yticklabels():
            tick.set_fontname(fontname)
        ax2.spines['top'].set_linewidth(.5)
        ax2.spines['right'].set_linewidth(.5)
        ax2.spines['left'].set_linewidth(.5)
        ax2.spines['bottom'].set_linewidth(.5)
    else:
        if hide_top_right_axes:
            ax.spines['right'].set_visible(False)
            ax.spines['top'].set_visible(False)

    if n_trials==1 and show_slope_counts:
        ax2 = ax.twinx()
        # scale y axis so the max count height is barchart_size of overall chart
        ax2.set_ylim(0, max(slope_counts_at_x) * 1/barchart_size)
        # draw just 0 and max count
        ax2.yaxis.set_major_locator(plt.FixedLocator([0, max(slope_counts_at_x)]))
        ax2.bar(x=pdpx, height=slope_counts_at_x, width=count_bar_width,
                facecolor=barchar_color, align='center', alpha=barchar_alpha)
        ax2.set_ylabel(f"slope count", labelpad=-12, fontsize=label_fontsize,
                       fontstretch='extra-condensed',
                       fontname=fontname)
        # shift other y axis down barchart_size to make room
        if yrange is not None:
            ax.set_ylim(yrange[0]-(yrange[1]-yrange[0])*barchart_size * leave_room_scaler, yrange[1])
        else:
            ax.set_ylim(min_y-(max_y-min_y)*barchart_size, max_y)
        ax2.tick_params(axis='both', which='major', labelsize=ticklabel_fontsize)
        for tick in ax2.get_xticklabels():
            tick.set_fontname(fontname)
        for tick in ax2.get_yticklabels():
            tick.set_fontname(fontname)
        ax2.spines['top'].set_linewidth(.5)
        ax2.spines['right'].set_linewidth(.5)
        ax2.spines['left'].set_linewidth(.5)
        ax2.spines['bottom'].set_linewidth(.5)

    if show_impact:
        ax.fill_between(pdpx, pdpy, [0] * len(pdpx), color=impact_fill_color)
        if show_impact_dots:
            ax.scatter(pdpx, pdpy, s=impact_marker_size, c=impact_pdp_color)
        if show_impact_line:
            ax.plot(pdpx, pdpy, lw=.3, c='grey')

    if show_xlabel:
        xl = colname
        if show_impact:
            xl += f" (Impact {np.mean(impact):.2f}, importance {np.mean(importance):.2f})"
        ax.set_xlabel(xl, fontsize=label_fontsize, fontname=fontname)
    if show_ylabel:
        ax.set_ylabel(targetname, fontsize=label_fontsize, fontname=fontname)
    if title is not None:
        ax.set_title(title, fontsize=title_fontsize, fontname=fontname)

    ax.spines['top'].set_linewidth(.5)
    ax.spines['right'].set_linewidth(.5)
    ax.spines['left'].set_linewidth(.5)
    ax.spines['bottom'].set_linewidth(.5)

    for tick in ax.get_xticklabels():
        tick.set_fontname(fontname)
    for tick in ax.get_yticklabels():
        tick.set_fontname(fontname)
    ax.tick_params(axis='both', which='major', labelsize=ticklabel_fontsize)

    return pdpx, pdpy, ignored


def finite_differences(x: np.ndarray, y: np.ndarray):
    """
    Use the unique x values within a leaf to compute finite differences. Given, n unique
    x values return n-1 derivative estimates. We start by grouping the leaf x,y by x
    and then collect the average y. The unique x and y averages are the new x and y pairs.

    The slope for each x is the forward discrete difference:

        (y_{i+1} - y_i) / (x_{i+1} - x_i)

    At right edge, there is no following x value so we could use backward difference:

        (y_i - y_{i-1}) / (x_{i+1} - x_i)

    But we don't use that last value as there's nothing to plot pass last value; slope
    at last value therefore is worthless.

    I experimented with the center difference using a secant method:

        (y_{i+1} - y_{i-1}) / (x_{i+1} - x_{i-1})

    which is Method A from Veldman, A.E.P., Rinzema, K. Playing with nonuniform grids.
    J Eng Math 26, 119–130 (1992).  https://doi.org/10.1007/BF00043231
    https://www.rug.nl/research/portal/files/3332271/1992JEngMathVeldman.pdf

    The np.gradient() function uses Method B from Veldman and Rinzema but that one seems
    to give much worse derivatives for sparse data.  In the end, forward difference seemed
    to get the best approximation for synthetic data, particularly sparse data. Since
    it is simplest, I went with it.

    If the ordinal/ints are exactly one unit part, then it's just y_{i+1} - y_i. If
    they are not consecutive, we do not ignore isolated x_i as it ignores too much data.
    E.g., if x is [1,3,4] and y is [9,8,10] then the x=2 coordinate is spanned as part
    of 1 to 3. The two slopes are [(8-9)/(3-1), (10-8)/(4-3)] and bin widths are [2,1].

    If there is exactly one unique x value in the leaf, the leaf provides no information
    about how X[colname] contributes to changes in y. We have to ignore this leaf.
    """
    ignored = 0

    # Group by x, take mean of all y with same x value (they come back sorted too)
    uniq_x = np.unique(x)
    avg_y = np.array([y[x==ux].mean() for ux in uniq_x])

    if len(uniq_x)==1:
        # print(f"ignore {len(x)} in discrete_xc_space")
        ignored += len(x)
        return np.array([[0]],dtype=x.dtype), np.array([0.0]), ignored

    # FORWARD DIFF
    x_deltas = np.diff(uniq_x)
    y_deltas = np.diff(avg_y)
    leaf_slopes = y_deltas / x_deltas  # "rise over run"

    # AVERAGE AROUND CENTER DIFF
    # At position i, take average of forward slope from y[i-1] to y[i] and
    # slope from y[i] to y[i+1].
    # leaf_slopes = [leaf_slopes[0]] + list((leaf_slopes[:-1] + leaf_slopes[1:]) / 2)
    # leaf_slopes = np.array(leaf_slopes)
    # dang, doesn't seem to work

    # CENTER DIFF
    # x_deltas2 = uniq_x[2:] - uniq_x[:-2]  # this is empty if |uniq_x|==2
    # y_deltas2 = avg_y[2:] - avg_y[:-2]
    # dydx0 = (avg_y[1] - avg_y[0]) / (uniq_x[1] - uniq_x[0]) # forward diff for i=0
    # leaf_slopes_ctr = [dydx0] + list(y_deltas2 / x_deltas2)  # "rise over run, stride 2"
    # leaf_slopes_ctr = np.array(leaf_slopes_ctr)

    # leaf_slopes = np.gradient(avg_y, uniq_x)[:-1] # drop last derivative as we won't use it
    # print("ctr",list(leaf_slopes_ctr))
    # print("grd",list(leaf_slopes))

    leaf_xranges = np.array(list(zip(uniq_x, uniq_x[1:])))

    return leaf_xranges, leaf_slopes, ignored


def collect_discrete_slopes(rf, X_col, X_not_col, y):
    """
    For each leaf of each tree of the decision tree or RF rf (trained on all features
    except colname), get the leaf samples then isolate the X[colname] values
    and the target y values.  Compute the y deltas between unique X[colname] values.
    Like performing piecewise linear regression of X[colname] vs y
    to get the slopes in various regions of X[colname].  We don't need to subtract
    the minimum y value before regressing because the slope won't be different.
    (We are ignoring the intercept of the regression line).

    Return for each leaf, the ranges of X[colname] partitions,
    associated slope for each range, and number of ignored samples.
    """
    # start = timer()
    leaf_slopes = []   # drop or rise between discrete x values
    leaf_xranges = []  # drop is from one discrete value to next

    ignored = 0

    leaves = leaf_samples(rf, X_not_col)
    y = y.values

    if False:
        nnodes = rf.estimators_[0].tree_.node_count
        print(f"Partitioning 'x not {colname}': {nnodes} nodes in (first) tree, "
              f"{len(rf.estimators_)} trees, {len(leaves)} total leaves")

    for samples in leaves:
        leaf_x = X_col[samples]
        # leaf_x = one_leaf_samples[]#.reshape(-1,1)
        leaf_y = y[samples]

        if np.abs(np.min(leaf_x) - np.max(leaf_x)) < 1.e-8: # faster than np.isclose()
            # print(f"ignoring xleft=xright @ {r[0]}")
            ignored += len(leaf_x)
            continue

        leaf_xranges_, leaf_slopes_, ignored_ = \
            finite_differences(leaf_x, leaf_y)

        leaf_slopes.extend(leaf_slopes_)
        leaf_xranges.extend(leaf_xranges_)
        ignored += ignored_

    if len(leaf_xranges)==0:
        # make sure empty list has same shape (jit complains)
        leaf_xranges = np.array([]).reshape(0, 0)
    else:
        leaf_xranges = np.array(leaf_xranges)
    leaf_slopes = np.array(leaf_slopes)

    # stop = timer()
    # if verbose: print(f"collect_discrete_slopes {stop - start:.3f}s")
    return leaf_xranges, leaf_slopes, ignored


# We get about 20% boost from parallel but limits use of other parallelism it seems;
# i get crashes when using multiprocessing package on top of this.
# If using n_jobs=1 all the time for importances, then turn jit=False so this
# method is not used
@jit(nopython=True, parallel=True) # use prange not range.
def avg_slopes_at_x_jit(uniq_x, leaf_ranges, leaf_slopes):
    """
    Compute the average of leaf_slopes at each uniq_x.

    Value at max(x) is NaN since we have no data beyond that point and so there is
    no forward difference. If last range is 4..5 then slope at 5 is nan since we
    don't know where it's going to go from there.
    """
    nx = uniq_x.shape[0]
    nslopes = leaf_slopes.shape[0]
    slopes = np.empty(shape=(nx, nslopes), dtype=np.double)
    for i in prange(nslopes):
        xr, slope = leaf_ranges[i], leaf_slopes[i]
        # Compute slope all the way across uniq_x but then trim line so
        # slope is only valid in range xr; don't set slope on right edge
        slopes[:, i] = np.where( (uniq_x < xr[0]) | (uniq_x >= xr[1]), np.nan, slope)

    # Slope values could be genuinely zero so we use nan not 0 for out-of-range.

    # Now average horiz across the matrix, averaging within each range
    # It's possible that some some rows would be purely NaN, indicating there are no
    # slopes for that X[colname] value. This can happen when we ignore some leaves,
    # when they have a single unique X[colname] value.

    # Compute:
    #   avg_slope_at_x = np.mean(slopes[good], axis=1)  (numba doesn't allow axis arg)
    #   slope_counts_at_x = nslopes - np.isnan(slopes).sum(axis=1)
    avg_slope_at_x = np.zeros(shape=nx)
    slope_counts_at_x = np.zeros(shape=nx)
    for i in prange(nx):
        row = slopes[i, :]
        n_nan = np.sum(np.isnan(row))
        avg_slope_at_x[i] = np.nan if n_nan==nslopes else np.nanmean(row)
        slope_counts_at_x[i] = nslopes - n_nan

    # return average slope at each unique x value and how many slopes included in avg at each x
    return avg_slope_at_x, slope_counts_at_x


# Hideous copying of avg_values_at_x_jit() to get different kinds of jit'ing. This is slower by 20%
# than other version but can run in parallel with multiprocessing package.
@jit(nopython=True)
def avg_slopes_at_x_nonparallel_jit(uniq_x, leaf_ranges, leaf_slopes):
    """
    Compute the weighted average of leaf_slopes at each uniq_x.

    Value at max(x) is NaN since we have no data beyond that point and so there is
    no forward difference.
    """
    nx = len(uniq_x)
    nslopes = len(leaf_slopes)
    slopes = np.zeros(shape=(nx, nslopes))
    for i in range(nslopes):
        xr, slope = leaf_ranges[i], leaf_slopes[i]
        # Compute slope all the way across uniq_x but then trim line so
        # slope is only valid in range xr; don't set slope on right edge
        slopes[:, i] = np.where( (uniq_x < xr[0]) | (uniq_x >= xr[1]), np.nan, slope)

    # Slope values could be genuinely zero so we use nan not 0 for out-of-range.

    # Now average horiz across the matrix, averaging within each range
    # It's possible that some some rows would be purely NaN, indicating there are no
    # slopes for that X[colname] value. This can happen when we ignore some leaves,
    # when they have a single unique X[colname] value.

    # Compute:
    #   avg_value_at_x = np.mean(slopes[good], axis=1)  (numba doesn't allow axis arg)
    #   slope_counts_at_x = nslopes - np.isnan(slopes).sum(axis=1)
    avg_value_at_x = np.zeros(shape=nx)
    slope_counts_at_x = np.zeros(shape=nx)
    for i in range(nx):
        row = slopes[i, :]
        n_nan = np.sum(np.isnan(row))
        avg_value_at_x[i] = np.nan if n_nan==nslopes else np.nanmean(row)
        slope_counts_at_x[i] = nslopes - n_nan

    # return average slope at each unique x value and how many slopes included in avg at each x
    return avg_value_at_x, slope_counts_at_x


def plot_stratpd_gridsearch(X, y, colname, targetname,
                            min_samples_leaf_values=(2,5,10,20,30),
                            min_slopes_per_x_values=(5,), # Show default count only by default
                            n_trials=1,
                            yrange=None,
                            xrange=None,
                            show_regr_line=False,
                            show_slope_lines=False,
                            show_impact=False,
                            show_slope_counts=False,
                            show_x_counts=True,
                            marginal_alpha=.05,
                            slope_line_alpha=.1,
                            pdp_marker_size=2,
                            title_fontsize=8,
                            label_fontsize=7,
                            ticklabel_fontsize=7,
                            cellwidth=2.5,
                            cellheight=2.5):
    ncols = len(min_samples_leaf_values)
    fig, axes = plt.subplots(len(min_slopes_per_x_values), ncols + 1,
                             figsize=((ncols + 1) * cellwidth, len(min_slopes_per_x_values)*cellheight))
    if len(min_slopes_per_x_values)==1:
        axes = axes.reshape(1,-1)
    for row,min_slopes_per_x in enumerate(min_slopes_per_x_values):
        marginal_plot_(X, y, colname, targetname, ax=axes[row][0],
                       show_regr_line=show_regr_line, alpha=marginal_alpha,
                       label_fontsize=label_fontsize,
                       ticklabel_fontsize=ticklabel_fontsize)
        col = 1
        axes[row][0].set_title("Marginal", fontsize=title_fontsize)
        for msl in min_samples_leaf_values:
            #print(f"---------- min_samples_leaf={msl} ----------- ")
            try:
                xrange_ = xrange
                if xrange is None:
                    xrange_ = (np.min(X[colname]), np.max(X[colname]))
                pdpx, pdpy, ignored = \
                    plot_stratpd(X, y, colname, targetname, ax=axes[row][col],
                                 min_samples_leaf=msl,
                                 min_slopes_per_x=min_slopes_per_x,
                                 n_trials=n_trials,
                                 xrange=xrange_,
                                 yrange=yrange,
                                 n_trees=1,
                                 show_ylabel=False,
                                 pdp_marker_size=pdp_marker_size,
                                 slope_line_alpha=slope_line_alpha,
                                 show_slope_lines=show_slope_lines,
                                 show_impact=show_impact,
                                 show_slope_counts=show_slope_counts,
                                 show_x_counts=show_x_counts,
                                 label_fontsize=label_fontsize,
                                 ticklabel_fontsize=ticklabel_fontsize)
                # print(f"leafsz {msl} avg abs curve value: {np.mean(np.abs(pdpy)):.2f}, mean {np.mean(pdpy):.2f}, min {np.min(pdpy):.2f}, max {np.max(pdpy)}")
            except ValueError as e:
                print(e)
                axes[row][col].set_title(f"Can't gen: leafsz={msl}", fontsize=8)
            else:
                title = f"leafsz={msl}, min_slopes={min_slopes_per_x}"
                if ignored>0:
                    title = f"leafsz={msl}, min_slopes={min_slopes_per_x},\nignored={100 * ignored / len(X):.2f}%"
                axes[row][col].set_title(title, fontsize=title_fontsize)
            col += 1


def marginal_plot_(X, y, colname, targetname, ax, alpha=.1, show_regr_line=True,
                   label_fontsize=7,
                   ticklabel_fontsize=7):
    ax.scatter(X[colname], y, alpha=alpha, label=None, s=10)
    ax.set_xlabel(colname, fontsize=label_fontsize)
    ax.set_ylabel(targetname, fontsize=label_fontsize)
    col = X[colname]

    ax.tick_params(axis='both', which='major', labelsize=ticklabel_fontsize)

    if show_regr_line:
        r = LinearRegression()
        r.fit(X[[colname]], y)
        xcol = np.linspace(np.min(col), np.max(col), num=100)
        yhat = r.predict(xcol.reshape(-1, 1))
        ax.plot(xcol, yhat, linewidth=1, c='orange', label=f"$\\beta_{{{colname}}}$")
        ax.text(min(xcol) * 1.02, max(y) * .95, f"$\\beta_{{{colname}}}$={r.coef_[0]:.3f}")


def marginal_catplot_(X, y, colname, targetname, ax, catnames, alpha=.1, show_xticks=True):
    catcodes, catnames_, catcode2name = getcats(X, colname, catnames)

    ax.scatter(X[colname].values, y.values, alpha=alpha, label=None, s=10)
    ax.set_xlabel(colname)
    ax.set_ylabel(targetname)
    # col = X[colname]
    # cats = np.unique(col)

    if show_xticks:
        ax.set_xticks(catcodes)
        ax.set_xticklabels(catnames_)
    else:
        ax.set_xticks([])

def plot_catstratpd_gridsearch(X, y, colname, targetname,
                               n_trials=1,
                               min_samples_leaf_values=(2, 5, 10, 20, 30),
                               min_y_shifted_to_zero=True,  # easier to read if values are relative to 0 (usually); do this for high cardinality cat vars
                               show_xticks=True,
                               show_impact=False,
                               show_all_cat_deltas=True,
                               catnames=None,
                               yrange=None,
                               cellwidth=2.5,
                               cellheight=2.5):

    ncols = len(min_samples_leaf_values)
    fig, axes = plt.subplots(1, ncols + 1,
                             figsize=((ncols + 1) * cellwidth, cellheight),
                             sharey=True)

    marginal_catplot_(X, y, colname, targetname, catnames=catnames, ax=axes[0], alpha=0.05,
                      show_xticks=show_xticks)
    axes[0].set_title("Marginal", fontsize=10)

    col = 1
    for msl in min_samples_leaf_values:
        #print(f"---------- min_samples_leaf={msl} ----------- ")
        if yrange is not None:
            axes[col].set_ylim(yrange)
        try:
            uniq_catcodes, combined_avg_per_cat, ignored, merge_ignored = \
                plot_catstratpd(X, y, colname, targetname, ax=axes[col],
                                n_trials=n_trials,
                                min_samples_leaf=msl,
                                catnames=catnames,
                                yrange=yrange,
                                n_trees=1,
                                show_impact=show_impact,
                                show_xticks=show_xticks,
                                show_ylabel=False,
                                min_y_shifted_to_zero=min_y_shifted_to_zero)
        except ValueError:
            axes[col].set_title(f"Can't gen: leafsz={msl}", fontsize=8)
        else:
            axes[col].set_title(f"leafsz={msl}, ign'd={ignored / len(X):.1f}%", fontsize=9)
        col += 1


def catwise_leaves(rf, X_not_col, X_col, y, max_catcode):
    """
    Return a 2D array with the average y value for each category in each leaf.
    Choose the smallest-valued cat code as the reference category (arbitrary)
    Normalize the y values into deltas by subtracting the avg y value for the
    reference category from the avg y for all categories.

    The columns are the y avg value changes per cat found in a single leaf as
    they differ from the reference cat y average. Each row represents a category level. E.g.,

    row
    cat           leaf0       leaf1
     0       166.430176  186.796956
     1       219.590349  176.448626

    Cats are possibly noncontiguous with nan rows for cat codes not present. Not all
    values in a leaf column will be non-nan.  Only those categories mentioned in
    a leaf have values.  Shape is (max cat + 1, num leaves).

    Within a single leaf, there will typically only be a few categories represented.
    """
    leaves = leaf_samples(rf, X_not_col)

    leaf_deltas = np.full(shape=(max_catcode+1, len(leaves)), fill_value=np.nan)
    leaf_counts = np.zeros(shape=(max_catcode+1, len(leaves)), dtype=int)
    keep_leaf_idxs = np.full(shape=(len(leaves),), fill_value=True, dtype=bool)

    ignored = 0
    for leaf_i in range(len(leaves)):
        sample = leaves[leaf_i]
        leaf_cats = X_col[sample]
        leaf_y = y[sample]
        # perform a groupby(catname).mean()
        uniq_leaf_cats, count_leaf_cats = np.unique(leaf_cats, return_counts=True) # comes back sorted
        avg_y_per_cat = np.array([leaf_y[leaf_cats==cat].mean() for cat in uniq_leaf_cats])
        # print("uniq_leaf_cats",uniq_leaf_cats,"count_y_per_cat",count_leaf_cats)

        if len(uniq_leaf_cats) < 2:
            # print(f"ignoring {len(sample)} obs for {len(avg_y_per_cat)} cat(s) in leaf")
            ignored += len(sample)
            keep_leaf_idxs[leaf_i] = False # we ignored this leaf
            continue

        # Use any cat code as refcat; same "shape" of delta vec regardless of which we
        # pick. The vector is shifted/up or down but cat y's all still have the same relative
        # delta y. Might as well just pick the first one. Previously, I picked a random
        # reference category but that is unnecessary. We will shift this vector during
        # the merge operation so which we pick here doesn't matter.
        idx_of_ref_cat_in_leaf = 0
        delta_y_per_cat = avg_y_per_cat - avg_y_per_cat[idx_of_ref_cat_in_leaf]
        # print("delta_y_per_cat",delta_y_per_cat)

        # Store into leaf i vector just those deltas we have data for
        # leave cats w/o representation as nan (uses index to figure out which rows to alter)
        leaf_deltas[uniq_leaf_cats, leaf_i] = delta_y_per_cat
        leaf_counts[uniq_leaf_cats, leaf_i] = count_leaf_cats

    # See unit test test_catwise_leaves:test_two_leaves_with_2nd_ignored()
    leaf_deltas = leaf_deltas[:,keep_leaf_idxs]
    leaf_counts = leaf_counts[:,keep_leaf_idxs]
    return leaf_deltas, leaf_counts, ignored


def cat_partial_dependence(X, y,
                           colname,  # X[colname] expected to be numeric codes
                           max_catcode=None,  # if we're bootstrapping, might see diff max's so normalize to one max
                           n_trees=1,
                           min_samples_leaf=5,
                           max_features=1.0,
                           rf_bootstrap=False,
                           supervised=True,
                           verbose=False):
    X_not_col = X.drop(colname, axis=1).values
    X_col = X[colname].values
    if (X_col<0).any():
        raise ValueError(f"Category codes must be > 0 in column {colname}")
    if not np.issubdtype(X_col.dtype, np.integer):
        raise ValueError(f"Category codes must be integers in column {colname} but is {X_col.dtype}")
    if max_catcode is None:
        max_catcode = np.max(X_col)
    if supervised:
        rf = RandomForestRegressor(n_estimators=n_trees,
                                   min_samples_leaf=min_samples_leaf,
                                   bootstrap = rf_bootstrap,
                                   max_features = max_features,
                                   oob_score=False)
        rf.fit(X_not_col, y)
        if verbose:
            print(f"CatStrat Partition RF: dropping {colname} training R^2 {rf.score(X_not_col, y):.2f}")
    else:
        print("USING UNSUPERVISED MODE")
        X_synth, y_synth = conjure_twoclass(X)
        rf = RandomForestClassifier(n_estimators=n_trees,
                                    min_samples_leaf=min_samples_leaf,  # * 2, # there are 2x as many samples (X,X') so must double leaf size
                                    bootstrap=rf_bootstrap,
                                    max_features=max_features,
                                    oob_score=False)
        rf.fit(X_synth.drop(colname,axis=1), y_synth)

    rf.fit(X_not_col, y)

    leaf_deltas, leaf_counts, ignored = \
        catwise_leaves(rf, X_not_col, X_col, y.values, max_catcode)

    USE_MEAN_Y=False
    if USE_MEAN_Y:
        count_per_cat = None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            avg_per_cat = np.nanmean(leaf_deltas, axis=1)
            merge_ignored = 0
            # slope_counts_at_cat = leaf_histos.shape[1] - np.isnan(leaf_histos).sum(axis=1)
    else:
        avg_per_cat, count_per_cat, merge_ignored = \
            avg_values_at_cat(leaf_deltas, leaf_counts, verbose=verbose)

    if verbose:
        print(f"CatStratPD Num samples ignored {ignored} for {colname}")

    return leaf_deltas, leaf_counts, avg_per_cat, count_per_cat, ignored, merge_ignored


def avg_values_at_cat(leaf_deltas, leaf_counts, max_iter=3, verbose=False):
    """
    In leaf_deltas, we have information from the leaves indicating how much
    above or below each category was from the reference category of that leaf.
    The reference category is arbitrarily selected to be the min cat code, so that
    refcat's relative y value in the leaf will be 0. Categories not mentioned
    in the leaf, will have NAN values.

    The goal is to merge all of the columns in leaf_deltas,
    despite the fact that they do not have the same reference category. We init a
    running average vector to be the first column of category deltas. Then we attempt
    to merge each of the other columns into the running average. We make multiple passes
    over the columns of leaf_deltas until nothing changes, we hit the maximum number
    of iterations, or everything has merged.

    To merge vector v (column j of leaf_deltas) into catavg, select a category,
    index ix, in common at random.  Subtract v[ix] from v so that ix is v's new
    reference and v[ix]=0. Add catavg[ix] to the adjusted v so that v is now
    comparable to catavg. We can now do a weighted average of catavg and v,
    paying careful attention of NaN.

    Previously, I was picking a random category for merging in an effort to reduce the effect
    of outliers, with the assumption that outliers were rare. Given 5 categories
    in common between the running average vector and a new vector, randomly picking one
    means a 1/5 chance of picking the outlier.  Outliers as reference categories shift
    the outlierness to all other categories. Boooo

    Now, I selected the category in common that has the most evidence: the category
    associated with the most number of observations.

    It's possible that more than a single value within a leaf_deltas vector is 0.
    I.e., the reference category value is always 0 in the vector, but there might be
    another category whose value was the same y, giving a 0 relative value.

    Example:

    leaf_deltas

    [[ 0. nan nan]
     [ 1. nan nan]
     [nan  0. nan]
     [nan  3.  0.]
     [nan  3.  2.]
     [ 4. nan nan]
     [ 5. nan nan]]

    leaf_counts

    [[1 0 0]
     [1 0 0]
     [0 1 0]
     [0 1 1]
     [0 1 1]
     [1 0 0]
     [1 0 0]]
    """
    # catavg is the running average vector and starts out as the first column (1st leaf's deltas)
    catavg = leaf_deltas[:,0] # init with first ref category (column)
    catavg_weight = leaf_counts[:,0]
    merge_ignored = 0

    work = set(range(1,leaf_deltas.shape[1])) # all leaf indexes added to work list
    completed = {-1} # init to any nonempty set to enter loop
    iteration = 1
    # Three passes should be sufficient to merge all possible vectors, but
    # I'm being paranoid here and allowing it to run until completion or some maximum iterations
    while len(work)>0 and len(completed)>0 and iteration<=max_iter:
        # print(f"PASS {iteration} len(work)", len(work))
        completed = set()
        for j in work:      # for remaining leaf index in work list, avg in the vectors
            v = leaf_deltas[:,j]
            are_intersecting = ~np.isnan(catavg) & ~np.isnan(v)
            intersection_idx = np.where(are_intersecting)[0]

            # print(intersection_idx)
            if len(intersection_idx)==0: # found something to merge into catavg?
                continue

            # cat for merging is the one with most supporting evidence
            cur_weight  = leaf_counts[:,j]
            ix = np.argmax(np.where((cur_weight>0) & are_intersecting, cur_weight, 0))

            # Merge column j into catavg vector
            shifted_v = v - v[ix]                       # make ix the reference cat in common
            relative_to_value = catavg[ix]              # corresponding value in catavg
            adjusted_v = shifted_v + relative_to_value  # adjust so v is mergeable with catavg
            prev_catavg = catavg                        # track only for verbose/debugging purposes
            catavg = nanavg_vectors(catavg, adjusted_v, catavg_weight, cur_weight)
            # Update weight of running avg to incorporate "mass" from v
            catavg_weight += cur_weight
            if verbose:
                print(f"{ix:-2d} : vec to add =", parray(v), f"- {v[ix]:.2f}")
                print("     shifted    =", parray(shifted_v), f"+ {relative_to_value:.2f}")
                print("     adjusted   =", parray(adjusted_v), "*", cur_weight)
                print("     prev avg   =", parray(prev_catavg),"*",catavg_weight-cur_weight)
                print("     new avg    =", parray(catavg))
                print()
            completed.add(j)
        iteration += 1
        work = work - completed

    if len(work)>0:
        #print(f"Left {len(work)} leaves/unique cats in work list")
        # hmm..couldn't merge some vectors; total up the samples we ignored
        num_obs_per_leaf = np.sum(leaf_counts, axis=0)
        merge_ignored = np.sum(num_obs_per_leaf[list(work)])
        if verbose: print(f"cats from {len(work)} leaves couldn't be merged into running avg; ignored={merge_ignored}")

    if verbose: print("final cat avgs", parray3(catavg))
    return catavg, catavg_weight, merge_ignored # last one is count of values per cat actually incorporated


def plot_catstratpd(X, y,
                    colname,  # X[colname] expected to be numeric codes
                    targetname,
                    catnames=None,
                    n_trials=1,
                    subsample_size = .75,
                    bootstrap=True,
                    ax=None,
                    n_trees=1,
                    rf_bootstrap=False,
                    min_samples_leaf=5,
                    max_features=1.0,
                    yrange=None,
                    title=None,
                    show_avg_pairwise_effect=False,  # show avg pairwise drop/bump to get to each cat from all others
                    show_x_counts=True,
                    show_all_pdp=True,
                    pdp_marker_size=6,
                    pdp_marker_alpha=.6,
                    pdp_color='#A5D9B5',
                    fontname='Arial',
                    title_fontsize=11,
                    label_fontsize=10,
                    barchart_size=0.20,
                    barchar_alpha=0.9,
                    ticklabel_fontsize=10,
                    min_y_shifted_to_zero=False,
                    leftmost_shifted_to_zero=False,  # either this or min_y_shifted_to_zero can be true
                    # easier to read if values are relative to 0 (usually); do this for high cardinality cat vars
                    show_xlabel=True,
                    show_xticks=True,
                    show_ylabel=True,
                    show_impact=False,
                    verbose=False,
                    figsize=(5,3)):
    """
    Plot the partial dependence of categorical variable X[colname] on y.
    Warning: cat columns are assumed to be label encoded as unique integers. This
    function uses the cat code as a raw index internally. So if you have two cat
    codes 1 and 1000, this function allocates internal arrays of size 1000+1.

    Key parameters:

    :param X: Dataframe with all explanatory variables

    :param y: Series or vector with response variable

    :param colname: which X[colname] (a string) to compute partial dependence for

    :param targetname: for plotting purposes, will what is the y axis label?

    :param catnames: dict or array mapping catcode to catname, used for plotting x axis

    :param n_trials:  How many times should we run the catstratpd algorithm and get PD
                      curves, using bootstrapped or subsample sets? Default is 1.

    :param min_samples_leaf Key hyper parameter to the stratification
                            process. The default is 5 and usually
                            works out pretty well.  It controls the
                            minimum number of observations in each
                            decision tree leaf used to stratify X other than colname.
                            Generally speaking, smaller values lead to more confidence
                            that fluctuations in y are due solely to
                            X[colname], but more observations per leaf allow
                            CatStratPD to capture more relationships and
                            make it less susceptible to noise. As the
                            leaf size grows, however, one risks
                            introducing contributions from X not colname into
                            the relationship between X[colname] and y. At the
                            extreme, the decision tree would consist
                            of a single leaf node containing all
                            observations, leading to a marginal not
                            partial dependence curve.
    """
    if ax is None:
        if figsize is not None:
            fig, ax = plt.subplots(1, 1, figsize=figsize)
        else:
            fig, ax = plt.subplots(1, 1)

    if show_avg_pairwise_effect:
        show_all_pdp = False

    uniq_catcodes = np.unique(X[colname])

    X_col = X[colname]
    n = len(X_col)

    ''' replaced with np.nanmean
    def avg_pd_catvalues(all_avg_per_cat):
        """For each unique catcode, sum and count avg_per_cat values found among trials"""
        m = np.zeros(shape=(max_catcode+1,))
        c = np.zeros(shape=(max_catcode+1,), dtype=int)
        for i in range(n_trials):
            avg_per_cat = all_avg_per_cat[i]
            catcodes = np.where(~np.isnan(avg_per_cat))[0]
            for code in catcodes:
                m[code] += avg_per_cat[code]
                c[code] += 1
        # Convert to average value per cat
        for code in np.where(m!=0)[0]:
            m[code] /= c[code]
        m = np.where(c==0, np.nan, m) # cats w/o values should be nan, not 0
        return m
    '''

    impacts = []
    importances = []
    all_avg_per_cat = []
    ignored = 0
    merge_ignored = 0
    for i in range(n_trials):
        if n_trials>1:
            if bootstrap:
                idxs = resample(range(n), n_samples=n, replace=True)
            else: # use subsetting
                idxs = resample(range(n), n_samples=int(n * subsample_size), replace=False)
            X_, y_ = X.iloc[idxs], y.iloc[idxs]
        else:
            X_, y_ = X, y

        leaf_deltas, leaf_counts, avg_per_cat, count_per_cat, ignored_, merge_ignored_ = \
            cat_partial_dependence(X_, y_,
                                   max_catcode=np.max(X_col),
                                   colname=colname,
                                   n_trees=n_trees,
                                   min_samples_leaf=min_samples_leaf,
                                   max_features=max_features,
                                   rf_bootstrap=rf_bootstrap,
                                   verbose=verbose)
        impact, importance = featimp.cat_compute_importance(avg_per_cat, count_per_cat)
        impacts.append(impact)
        importances.append(importance)
        ignored += ignored_
        merge_ignored += merge_ignored_
        all_avg_per_cat.append( avg_per_cat )

    all_avg_per_cat = np.array(all_avg_per_cat)

    if leftmost_shifted_to_zero:
        all_avg_per_cat -= all_avg_per_cat[np.isfinite(all_avg_per_cat)][0]
    if min_y_shifted_to_zero:
        all_avg_per_cat -= np.nanmin(all_avg_per_cat)

    ignored /= n_trials # average number of x values ignored across trials
    merge_ignored /= n_trials # average number of x values ignored across trials

    # average down the matrix of all_avg_per_cat across trials to get average per cat
    # combined_avg_per_cat = avg_pd_catvalues(all_avg_per_cat)
    if n_trials>1:
        combined_avg_per_cat = np.nanmean(all_avg_per_cat, axis=0)
    else:
        combined_avg_per_cat = all_avg_per_cat.flatten()
    # print("start of combined_avg_per_cat =", combined_avg_per_cat[uniq_catcodes][0:20])
    # print("mean(pdpy)", np.nanmean(combined_avg_per_cat))

    if show_avg_pairwise_effect:
        combined_avg_per_cat = featimp.all_pairs_delta(combined_avg_per_cat)

    impact_order = np.argsort(impacts)
    # print("impacts", impacts)
    # print("avg impact", np.mean(impacts))

    cmap = plt.get_cmap('coolwarm')
    colors=cmap(np.linspace(0, 1, num=n_trials))
    min_y = 9999999999999
    max_y = -min_y

    for i in range(0,n_trials): # find min/max from all trials
        avg_per_cat = all_avg_per_cat[i]
        if np.nanmin(avg_per_cat) < min_y:
            min_y = np.nanmin(avg_per_cat)
        if np.nanmax(avg_per_cat) > max_y:
            max_y = np.nanmax(avg_per_cat)

    # Show a dot for each cat in all trials
    if show_all_pdp and n_trials>1:
        for i in range(1,n_trials): # only do if > 1 trial
            ax.plot(range(len(uniq_catcodes)), all_avg_per_cat[i][uniq_catcodes], '.', c=mpl.colors.rgb2hex(colors[impact_order[i]]),
                    markersize=pdp_marker_size, alpha=pdp_marker_alpha)

    '''
    # Show avg line
    segments = []
    for cat, delta in zip(range(len(uniq_catcodes)), combined_avg_per_cat[uniq_catcodes]):
        one_line = [(cat-0.5, delta), (cat+0.5, delta)]
        segments.append(one_line)
        # ax.plot([cat-0.5,cat+0.5], [delta,delta], '-',
        #         lw=1.0, c=pdp_color, alpha=pdp_marker_alpha)
        # ax.plot(range(len(uniq_catcodes)), avg_delta, '.', c='k', markersize=pdp_marker_size + 1)
    lines = LineCollection(segments, alpha=pdp_marker_alpha, color=pdp_color, linewidths=pdp_marker_lw)
    ax.add_collection(lines)
    '''

    barcontainer = ax.bar(x=range(len(uniq_catcodes)),
                          height=combined_avg_per_cat[uniq_catcodes],
                          color=pdp_color)
    # Alter appearance of each bar
    for rect in barcontainer.patches:
        rect.set_linewidth(.1)
        rect.set_edgecolor('#444443')

    leave_room_scaler = 1.3

    if yrange is not None:
        ax.set_ylim(*yrange)
    else:
        ax.set_ylim(min_y*1.05, max_y*1.05)

    if show_x_counts:
        # Only show cat counts for those which are present in X[colname] (unlike stratpd plot)
        _, cat_counts = np.unique(X_col[np.isin(X_col, uniq_catcodes)], return_counts=True)
        count_bar_width=1
        ax2 = ax.twinx()
        # scale y axis so the max count height is 10% of overall chart
        ax2.set_ylim(0, max(cat_counts) * 1/barchart_size)
        # draw just 0 and max count
        ax2.yaxis.set_major_locator(plt.FixedLocator([0, max(cat_counts)]))
        ax2.bar(x=range(len(uniq_catcodes)), height=cat_counts, width=count_bar_width,
                facecolor='#BABABA', align='center', alpha=barchar_alpha)
        ax2.set_ylabel(f"PD $x$ point count\nignored={ignored:.0f}, {merge_ignored:.0f}", labelpad=-12, fontsize=label_fontsize,
                       fontstretch='extra-condensed',
                       fontname=fontname)
        # shift other y axis down barchart_size to make room
        if yrange is not None:
            ax.set_ylim(yrange[0]-(yrange[1]-yrange[0])*barchart_size * leave_room_scaler, yrange[1])
        else:
            ax.set_ylim(min_y-(max_y-min_y)*barchart_size * leave_room_scaler, max_y)
        plt.setp(ax2.get_xticklabels(), visible=False)
        for tick in ax2.get_yticklabels():
            tick.set_fontname(fontname)
        ax2.spines['top'].set_linewidth(.5)
        ax2.spines['right'].set_linewidth(.5)
        ax2.spines['left'].set_linewidth(.5)
        ax2.spines['bottom'].set_linewidth(.5)

    ax.tick_params(axis='both', which='major', labelsize=ticklabel_fontsize)

    # leave .8 on either size of graph
    ax.set_xlim(0-.8,len(uniq_catcodes)-1+0.8)
    if show_xticks:
        ax.set_xticks(range(len(uniq_catcodes)))
        if catnames is not None:
            labels = [catnames[c] for c in uniq_catcodes]
            ax.set_xticklabels(labels)
        else:
            ax.set_xticklabels(uniq_catcodes)
        for tick in ax.get_xticklabels():
            tick.set_fontname(fontname)
    else:
        ax.set_xticks([])
        ax.set_xticklabels([])

    ax.spines['left'].set_smart_bounds(True)
    ax.spines['bottom'].set_smart_bounds(True)
    ax.spines['right'].set_visible(False)
    ax.spines['top'].set_visible(False)

    if show_xlabel:
        label = colname
        if show_impact:
            label += f" (Impact {np.nanmean(np.abs(combined_avg_per_cat)):.2f})"
        ax.set_xlabel(label, fontsize=label_fontsize, fontname=fontname)
    if show_ylabel:
        ax.set_ylabel(targetname, fontsize=label_fontsize, fontname=fontname)
    if title is not None:
        ax.set_title(title, fontsize=title_fontsize, fontname=fontname)

    for tick in ax.get_yticklabels():
        tick.set_fontname(fontname)

    if title is not None:
        ax.set_title(title, fontsize=title_fontsize, fontname=fontname)

    ax.spines['left'].set_linewidth(.5)
    ax.spines['bottom'].set_linewidth(.5)

    return uniq_catcodes, combined_avg_per_cat, ignored, merge_ignored


def getcats(X, colname, incoming_cats):
    if incoming_cats is None or isinstance(incoming_cats, pd.Series):
        catcodes = np.unique(X[colname])
        catcode2name = [None] * (max(catcodes) + 1)
        for c in catcodes:
            catcode2name[c] = c
        catcode2name = np.array(catcode2name)
        catnames = catcodes
    elif isinstance(incoming_cats, dict):
        catnames_ = [None] * (max(incoming_cats.keys()) + 1)
        catcodes = []
        catnames = []
        for code, name in incoming_cats.items():
            catcodes.append(code)
            catnames.append(name)
            catnames_[code] = name
        catcodes = np.array(catcodes)
        catnames = np.array(catnames)
        catcode2name = np.array(catnames_)
    elif not isinstance(incoming_cats, dict):
        # must be a list of names then
        catcodes = []
        catnames_ = [None] * len(incoming_cats)
        for cat, c in enumerate(incoming_cats):
            if c is not None:
                catcodes.append(cat)
            catnames_[cat] = c
        catcodes = np.array(catcodes)
        catcode2name = np.array(catnames_)
        catnames = np.array(incoming_cats)
    else:
        raise ValueError("catnames must be None, 0-indexed list, or pd.Series")
    return catcodes, catnames, catcode2name


# -------------- S U P P O R T ---------------

def scramble(X : np.ndarray) -> np.ndarray:
    """
    From Breiman: https://www.stat.berkeley.edu/~breiman/RandomForests/cc_home.htm
    "...the first coordinate is sampled from the N values {x(1,n)}. The second
    coordinate is sampled independently from the N values {x(2,n)}, and so forth."
    """
    X_rand = X.copy()
    ncols = X.shape[1]
    for col in range(ncols):
        X_rand[:,col] = np.random.choice(X[:,col], len(X), replace=True)
    return X_rand


def df_scramble(X : pd.DataFrame) -> pd.DataFrame:
    """
    From Breiman: https://www.stat.berkeley.edu/~breiman/RandomForests/cc_home.htm
    "...the first coordinate is sampled from the N values {x(1,n)}. The second
    coordinate is sampled independently from the N values {x(2,n)}, and so forth."
    """
    X_rand = X.copy()
    for colname in X:
        # X_rand[colname] = np.random.choice(X[colname], len(X), replace=True)
        X_rand[colname] = X_rand[colname].sample(frac=1.0)
    return X_rand


def conjure_twoclass(X):
    """
    Make new data set 2x as big with X and scrambled version of it that
    destroys structure between features. Old is class 0, scrambled is class 1.
    """
    if isinstance(X, pd.DataFrame):
        X_rand = df_scramble(X)
        X_synth = pd.concat([X, X_rand], axis=0)
    else:
        X_rand = scramble(X)
        X_synth = np.concatenate([X, X_rand], axis=0)
    y_synth = np.concatenate([np.zeros(len(X)),
                              np.ones(len(X_rand))], axis=0)
    return X_synth, pd.Series(y_synth)


def compress_catcodes(X, catcolnames, inplace=False):
    "Compress categorical integers if less than 90% dense"
    X_ = X if inplace else X.copy()
    for colname in catcolnames:
        uniq_x = np.unique(X_[colname])
        if len(uniq_x) < 0.90 * len(X_):  # sparse? compress into contiguous range of x cat codes
            X_[colname] = X_[colname].rank(method='min').astype(int)
    return X_


def nanavg_vectors(a, b, wa=1.0, wb=1.0):
    """
    Add two vectors a+b but support nan+x==x and nan+nan=nan
    np.nanmean works to get nan+nan=nan, but for weighted avg
    we need to divide by wa+wb after using nansum. nansum gives
    0 not nan it seems when adding nan+nan. Do it the hard way.
    """
    a_nan = np.isnan(a)
    b_nan = np.isnan(b)
    c = a*wa + b*wb               # weighted average where both are non-nan
    c /= zero_as_one(wa+wb)       # weighted avg
    c[a_nan] = b[a_nan]           # copy any stuff where b has only value (unweighted into result)
    in_a_not_b = (~a_nan) & b_nan
    c[in_a_not_b] = a[in_a_not_b] # copy stuff where a has only value
    return c


def nanmerge_matrix_cols(A):
    """
    Add all vertical vectors in A but support nan+x==x and nan+nan=nan.
    """
    s = np.nansum(A, axis=1)
    all_nan_entries = np.isnan(A)
    # if all entries for a cat are nan, make sure sum s is nan for that cat
    s[all_nan_entries.all(axis=1)] = np.nan
    return s


def zero_as_one(a):
    return np.where(a == 0, 1, a)


def parray(a):
    if type(a[0])==np.int64:
        return '[ ' + (' '.join([f"{x:6d}" for x in a])).strip() + ' ]'
    else:
        return '[ ' + (' '.join([f"{x:6.2f}" for x in a])).strip() + ' ]'


def parray3(a):
    return '[ ' + (' '.join([f"{x:6.3f}" for x in a])).strip() + ' ]'
