<p align="center">
  <a href="README.md">English</a> | 简体中文
</p>

# 闲鱼搜索（goofish）

> <sub>本文档由 Anthropic 的 Claude Fable 5.2 生成。</sub>

这是 Miyu 的一个脚本软件包，提供一件工具 `goofish`。它不需要登录就能搜索闲鱼上的二手商品，返回价格、原价、成色和品牌标签、地区、想要人数、发布日期、卖家和链接；它也可以盯住一个关键词，之后每次只需要几秒就能查询一次，并标出以前没有见过的新商品。

## 参数

这件工具接受下面这些参数：

| 参数 | 说明 |
|---|---|
| `action` | `search`（默认）打开无头浏览器搜索，需要 10–40 秒；`monitor-init` 打开一次浏览器，为关键词建立监控；`monitor-query` 不打开浏览器，几秒就能查询一次监控，新出现的商品标上 🆕 并排在前面；`monitor-list`、`monitor-remove` 分别用来查看和删除监控；`doctor` 进行自检 |
| `keyword` | 要搜索的关键词。除了 `monitor-list` 和 `doctor`，其他操作都需要填写 |
| `pages` | 只用于 `search`：获取的页数，1–5，每页 30 条，每多一页大约多花 10 秒。默认是 1 |
| `sort` | `default`（闲鱼的顺序）、`price_asc`、`price_desc`、`newest`、`most_wanted` |
| `min_price`、`max_price` | 价格范围，单位是元 |
| `limit` | 最多列出的条数，1–150，默认 30 |
| `format` | `md`（默认）或者 `json`（供 `miyu call` 里的代码使用） |

排序和价格筛选只作用于取回来的那几页，而不是全站的商品：如果想要更全的结果，就增加 `pages`。结果里的广告位已经去掉。

## 依赖

这件工具需要 Python 3.8+、[playwright](https://playwright.dev/python/) 和一个 Chromium 系的浏览器。可以使用：

```sh
pip install --user playwright          # Arch：pacman -S python-playwright
python3 -m playwright install chromium  # 或者装系统的 Chromium / Chrome / Edge，脚本自己找
```

安装完成以后，让 AI 调用一次 `action=doctor`，它会实际启动一次无头浏览器来验证。

## 沙盒和存放

这一节说明工具把数据存放在哪里，以及它在 Miyu 的沙盒里怎样运行。

- 监控保存在 Miyu 给脚本的状态目录 `MIYU_STATE_DIR` 里：它按 Miyu 账号分开保存，重启以后仍然存在。
- 缓存目录 `MIYU_CACHE_DIR` 里放的是可以丢弃的内容：浏览器会话快照（25 分钟内会重复使用，以减少触发风控）和浏览器自己的配置目录（`browser-home/`）。脚本不会写入其他任何位置。
- Miyu 的沙盒把 `~/.config/chromium`、`~/.config/google-chrome` 替换成空的只读目录，`/dev/shm` 也无法写入；所以脚本给浏览器另外设置了 `XDG_CONFIG_HOME`，加上了 `--disable-dev-shm-usage`，在 Linux 上还加了 `--no-sandbox`（因为 Chromium 自己的沙盒无法套进 Miyu 的沙盒里）。在 Linux 上，Landlock 和 bubblewrap 挂载这两种方式都经过实测，可以正常运行。macOS 和 Windows 还没有经过实测。
- 缓存目录位于系统临时目录下，重启以后会被清掉：会话快照丢失只会让下一次重新建立，监控不受影响。

## 风控

这一节说明闲鱼拦截请求时工具会怎样处理。

闲鱼会间歇性地拦截请求（RGV587，提示 `被挤爆啦`），脚本会自己重试，最长约 40 秒；如果仍然失败就报错，这时等 3 分钟左右再试。监控的原理是把建立监控那一次的请求原样重新发送，所以会话过期以后请求会被拒绝，报错里会提示你重新运行 `monitor-init`。
