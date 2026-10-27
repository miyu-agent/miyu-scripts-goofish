# 闲鱼（miyu-scripts-goofish）

给 Miyu 的一件脚本工具 `goofish`：免登录搜闲鱼二手，交回价格、原价、成色/品牌标签、地区、想要人数、发布日期、卖家和链接；也能盯住一个关键词，之后几秒就查一次，标出没见过的新商品。

## 参数

| 参数 | 说明 |
|---|---|
| `action` | `search`（默认）开无头浏览器搜，10–40 秒；`monitor-init` 开一次浏览器，给关键词开监控；`monitor-query` 不开浏览器、几秒查一次监控，新出现的标 🆕 排在前面；`monitor-list`、`monitor-remove` 看、删监控；`doctor` 自检 |
| `keyword` | 搜什么。`monitor-list`、`doctor` 以外都要 |
| `pages` | 只给 `search`：取几页，1–5，每页 30 条，每多一页多约 10 秒。默认 1 |
| `sort` | `default`（闲鱼的顺序）、`price_asc`、`price_desc`、`newest`、`most_wanted` |
| `min_price`、`max_price` | 价格范围（元） |
| `limit` | 至多列几条，1–150，默认 30 |
| `format` | `md`（默认）或 `json`（给 `miyu call` 里的代码用） |

排序和价格筛选只作用在取回来的那几页上，不是全站：要更全就加 `pages`。广告位已经去掉。

## 依赖

要 Python 3.8+、[playwright](https://playwright.dev/python/) 和一个 Chromium 系浏览器：

```sh
pip install --user playwright          # Arch：pacman -S python-playwright
python3 -m playwright install chromium  # 或者装系统的 Chromium / Chrome / Edge，脚本自己找
```

装好后让 AI 调一次 `action=doctor`，会真起一次无头浏览器验证。

## 沙盒和存放

- 监控存在 Miyu 给脚本的状态目录 `MIYU_SCRIPT_STATE_DIR`：按 Miyu 账号分开、重启还在。
- 缓存目录 `MIYU_SCRIPT_CACHE_DIR` 放能丢的：浏览器会话快照（25 分钟内复用，少碰风控）、浏览器自己的配置目录（`browser-home/`）。脚本别处都不写。
- Miyu 的沙盒把 `~/.config/chromium`、`~/.config/google-chrome` 盖成空的只读目录，`/dev/shm` 也写不了；脚本给浏览器另设了 `XDG_CONFIG_HOME`、加了 `--disable-dev-shm-usage`，Linux 上加了 `--no-sandbox`（Chromium 自己的沙盒套不进 Miyu 的）。Linux 上 Landlock 和 bubblewrap 挂载两种都实测能跑。macOS、Windows 没实测。
- 缓存目录在系统临时目录下，重启会清掉：会话快照没了只是下次重建，监控不受影响。

## 风控

闲鱼会间歇拦下请求（RGV587「被挤爆啦」），脚本自己重试到约 40 秒；还不行就报错，等 3 分钟左右再试。监控是把开监控那一次的请求原样重发，会话过期后会被拒，报错里会说重新 `monitor-init`。
