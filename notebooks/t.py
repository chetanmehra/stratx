import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression, Lasso

from stratx import featimp
from stratx.partdep import *
from rfpimp import plot_importances
import rfpimp

import shap

palette = [
    "#a6cee3",
    "#1f78b4",
    "#b2df8a",
    "#33a02c",
    "#fb9a99",
    "#e31a1c",
    "#fdbf6f",
    "#ff7f00",
    "#cab2d6",
    "#6a3d9a",
    "#ffff99",
    "#b15928"
]
def plot_all_PD(X,y,eqn=None,min_samples_leaf=5, mean_center=False):
    fig, axes = plt.subplots(2,1)
    for i,colname in enumerate(X.columns):
        plot_stratpd(X, y, colname, 'y', ax=axes[0], min_samples_leaf=min_samples_leaf,
                     show_slope_lines=False,
                     pdp_marker_color=palette[i])
#     uniq_x = np.array(sorted(np.unique(X[:,0])))
#     ax2 = ax.twinx()
    record_x = range(len(y))
    #record_x = (record_x - np.mean(record_x)) / np.std(record_x)
    yplot = np.array(sorted(y))
    print("min y", np.min(y))
    yplot = y
    ax = axes[1]
    ax.plot(record_x, yplot, lw=.3, c='k',
            label='marginal $y$ vs $x^{(i)}$')
    ax.plot([min(record_x),max(record_x)], [np.mean(np.abs(y)), np.mean(np.abs(yplot))],
            lw=1, c='orange', label="mean abs marginal")
    if mean_center:
        yplot = yplot-np.mean(y)
        ax.plot(record_x, yplot, lw=.3, c='k')
    ax.plot([min(record_x),max(record_x)], [np.mean(np.abs(yplot)), np.mean(np.abs(yplot))],
            lw=1, c='orange', label="mean abs marginal")
    print("plot: mean abs y", np.mean(np.abs(y)))
    print("plot: mean abs 0-centered y", np.mean(np.abs(y-np.mean(y))))
    print("plot: mean abs shifted y", np.mean(np.abs(y-np.min(y))))

    leaf_xranges, leaf_slopes, pdpx, pdpy, ignored = \
        PD(X=X, y=y, colname='x1')
    pdpx *= (len(record_x) / (max(pdpx) - min(pdpx)))
    ax.scatter(pdpx, pdpy, s=3)

    if eqn is not None:
        plt.title(f"${eqn}$")
    plt.tight_layout()
    # plt.legend()
    plt.show()


def synthetic_poly_data(n, p):
    df = pd.DataFrame()
    # Add independent x variables in [0.0, 1.0)
    coeff = np.random.random_sample(size=p)*10 # get p random coefficients
    coeff = np.array([2,3,4])
    coeff = np.array([3,4])
    for i in range(p):
        df[f'x{i+1}'] = np.round(np.random.random_sample(size=n)*10,1)
    # df['x3'] = df['x1']  # copy x1
    # multiply coefficients x each column (var) and sum along columns
    yintercept = 0
    df['y'] = np.sum( [coeff[i]*df[f'x{i+1}'] for i in range(p)], axis=0 ) + yintercept
    #TODO add noise
    terms = [f"{coeff[i]:.1f}x_{i+1}" for i in range(p)] + [f"{yintercept:.0f}"]
    eqn = "y = " + '+'.join(terms)
    return df, coeff, eqn

"""
It looks like I need to subtract out the y-intercept and not the average. 
We are really trying to normalize the Y so that it starts at 0, just like the
PDP y's.  Summing the PDP abs(y-yintercept) gives almost exactly mean(y)-yintercept.
"""

df, coeff, eqn = synthetic_poly_data(200,2)
print(df.head(3))

X = df.drop('y', axis=1)
y = df['y']
#X = featimp.standardize(X)

lr = LinearRegression()
lr.fit(X,y)
print("intercept is ", lr.intercept_)

rf = RandomForestRegressor(n_estimators=10)
rf.fit(X,y)

I = featimp.importances(X, y)
plot_importances(I, imp_range=(0, 1))

leftx1 = df.groupby('x1').mean()['y'].iloc[0]
print('leftmost x1 avg', leftx1)
leftx2 = df.groupby('x2').mean()['y'].iloc[0]
print('leftmost x2 avg', leftx2)
print("sum", leftx1+leftx2)

df_ = df.sort_values('x1')
print(df_.head(10))
y = df_['y']
X = df_.drop('y', axis=1)

plot_all_PD(X,y,eqn,mean_center=False)