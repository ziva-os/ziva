"""正态分布（Normal Distribution）示意图"""
import numpy as np
import matplotlib.pyplot as plt
import matplotlib as mpl

# ---------- 中文字体 ----------
mpl.rcParams['font.sans-serif'] = ['PingFang SC', 'Heiti SC', 'STHeiti', 'Arial Unicode MS', 'SimHei', 'DejaVu Sans']
mpl.rcParams['axes.unicode_minus'] = False

# ---------- 数据 ----------
mu, sigma = 0, 1
x = np.linspace(mu - 4 * sigma, mu + 4 * sigma, 1000)
y = (1 / (sigma * np.sqrt(2 * np.pi))) * np.exp(-0.5 * ((x - mu) / sigma) ** 2)

# ---------- 画布 ----------
fig, ax = plt.subplots(figsize=(11, 6.5), dpi=140)
fig.patch.set_facecolor('#fafafa')
ax.set_facecolor('#fafafa')

# ---------- 曲线 ----------
ax.plot(x, y, color='#2b6cb0', linewidth=2.6, label=r'$\mathcal{N}(\mu,\sigma^2)$')

# 填充三个区域：μ-σ~μ+σ、μ-2σ~μ-σ、μ+σ~μ+2σ
ax.fill_between(x, y, where=(x >= mu - sigma) & (x <= mu + sigma),
                color='#2b6cb0', alpha=0.28, label=r'$P(\mu-\sigma \leq X \leq \mu+\sigma) \approx 68.27\%$')
ax.fill_between(x, y, where=((x >= mu - 2 * sigma) & (x <= mu - sigma)) |
                              ((x >= mu + sigma) & (x <= mu + 2 * sigma)),
                color='#38a169', alpha=0.22, label=r'$P(\mu-2\sigma \leq X \leq \mu+2\sigma) \approx 95.45\%$')
ax.fill_between(x, y, where=(x < mu - 2 * sigma) | (x > mu + 2 * sigma),
                color='#e53e3e', alpha=0.18, label=r'$P(|X-\mu|>2\sigma) \approx 4.55\%$')

# ---------- 参考线 ----------
for v in [mu - 2 * sigma, mu - sigma, mu, mu + sigma, mu + 2 * sigma]:
    ax.axvline(v, color='#a0aec0', linestyle='--', linewidth=0.8, alpha=0.7)
ax.axhline(0, color='#cbd5e0', linewidth=0.8)

# ---------- 标注 ----------
# x 轴刻度标签
labels = [r'$\mu-2\sigma$', r'$\mu-\sigma$', r'$\mu$', r'$\mu+\sigma$', r'$\mu+2\sigma$']
ax.set_xticks([mu - 2 * sigma, mu - sigma, mu, mu + sigma, mu + 2 * sigma])
ax.set_xticklabels(labels, fontsize=12)

# 峰值标注
y_max = y.max()
ax.annotate(r'峰 = $\dfrac{1}{\sigma\sqrt{2\pi}}$',
            xy=(mu, y_max), xytext=(mu + 1.1, y_max * 0.85),
            fontsize=13, color='#2b3748',
            arrowprops=dict(arrowstyle='->', color='#718096', lw=1.2))

# 拐点标注
infl_x = mu - sigma
infl_y = y[abs(x - infl_x).argmin()]
ax.annotate(r'拐点 $(\mu-\sigma,\ \frac{1}{\sigma\sqrt{2\pi e}})$',
            xy=(infl_x, infl_y), xytext=(mu - 3.4, infl_y + 0.05),
            fontsize=11, color='#2d3748',
            arrowprops=dict(arrowstyle='->', color='#718096', lw=1.1))

# ---------- 坐标轴样式 ----------
ax.set_xlabel('X', fontsize=12)
ax.set_ylabel(r'概率密度 $f(x)$', fontsize=12)
ax.set_title('正态分布（Normal Distribution）', fontsize=18, pad=14, color='#1a202c', weight='bold')
ax.legend(loc='upper right', fontsize=10, frameon=True, facecolor='white', edgecolor='#cbd5e0')
ax.grid(True, linestyle=':', alpha=0.4)
ax.set_xlim(mu - 4 * sigma, mu + 4 * sigma)
ax.set_ylim(0, y_max * 1.18)
for spine in ['top', 'right']:
    ax.spines[spine].set_visible(False)

# ---------- 公式文本框 ----------
formula = (r'$f(x)=\dfrac{1}{\sigma\sqrt{2\pi}}\;'
           r'e^{-\frac{(x-\mu)^2}{2\sigma^2}}$')
ax.text(mu - 4, y_max * 1.05, formula, fontsize=14, color='#1a202c',
        bbox=dict(boxstyle='round,pad=0.5', facecolor='white',
                  edgecolor='#cbd5e0', alpha=0.95))

plt.tight_layout()
out = '/Users/wangxinxin/code/ziva/normal_distribution.png'
plt.savefig(out, dpi=160, bbox_inches='tight', facecolor=fig.get_facecolor())
print('saved ->', out)