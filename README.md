# Daily CV Paper Watch

面向水下图像增强研究的每日论文采集器。采集 arXiv `cs.CV`、CVF Open Access 和 OpenReview，保存到 SQLite，并生成中文 Markdown 日报与 JSON 数据。

## 本次升级

- **请求恢复**：对限流、超时和临时服务错误进行指数退避重试；支持 `Retry-After` 秒数与 HTTP 日期。不会重试 404 等永久错误，也不会在服务要求长时间等待时提前重试。
- **arXiv 降级**：API 不可用时尝试官方每日 cs.CV Atom 源，并在日报中明确标记。备用源仅覆盖当前公告批次，不能补齐搜索历史；两者都失败时记录来源失败。
- **CVF 增量采集**：成功解析的详情默认缓存 7 天；所有已发现会场的水下相关标题优先处理。缓存命中不占详情请求预算；失败请求也计入预算，连续失败 3 次后停止本次详情抓取。
- **OpenReview v2 + v1**：优先查询 v2，v2 搜索全部失败时回退 v1，兼容两种内容格式，过滤回复和已删除条目。请求统一使用可配置的超时、限速和重试，无需账号或 SDK。
- **任务优先筛选**：先判断增强、复原、颜色、低照度或成像物理任务，再考虑 Diffusion、Mamba、Transformer、会议与代码加分。仅有架构词、会议名、低照度场景或代码链接的论文不入选。
- **可解释推荐**：报告展示加分项；缺少摘要时限制推荐等级。OpenReview 中仅“投稿至某会议”不获接收状态加分。推荐是规则筛选结果，方法线索与论文创新需要阅读全文核对。
- **稳定去重**：优先匹配来源标识，arXiv 版本号不再产生新论文；兼容原库带版本的标识。合并多来源元数据后重新评分，旧分数允许降低，更新时间取较新值。
- **运行健康状态**：日报列出各来源的正常、降级或失败状态，原始/通过筛选条数和耗时。所有来源失败时退出码为 `1`，保留已有日报，并留下诊断文件。
- **每日新增模式**：定时任务仅展示今日新增；同日再次运行仍保留当天此前采集的新增论文。历史论文继续保存在数据库中。

## 运行

需要 Python **3.11 或更新版本**。Linux/macOS 使用标准库即可；Windows 的时区数据由 `requirements.txt` 安装。

```bash
python -m pip install -r requirements.txt
python scripts/collect_cv_papers.py --new-only
```

完整示例：

```bash
python scripts/collect_cv_papers.py \
  --db papers.db \
  --reports-dir reports \
  --sources arxiv,cvf,openreview \
  --max-per-source 80 \
  --openreview-limit-per-term 8 \
  --max-cvf-venues 8 \
  --max-cvf-detail-pages 90 \
  --http-timeout 30 \
  --http-retries 2 \
  --min-score 12 \
  --timezone Asia/Hong_Kong \
  --new-only
```

Windows PowerShell 请把上述命令写成一行，或使用 PowerShell 的续行语法。

### 常用选项

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `--sources` | `arxiv,cvf,openreview` | 逗号分隔的来源，名称错误会直接报错 |
| `--max-per-source` | `80` | arXiv 最大返回量；备用源先筛选再截断 |
| `--openreview-limit-per-term` | `8` | 每个关键词的最大条数 |
| `--max-cvf-venues` | `8` | 最多发现的 CVF 会场入口 |
| `--max-cvf-detail-pages` | `90` | CVF 详情请求次数预算，缓存命中不占用 |
| `--http-timeout` | `30` | 每次 HTTP 尝试的超时秒数 |
| `--http-retries` | `2` | 初次尝试之后的重试次数，最大 `5` |
| `--arxiv-delay` | `3.2` | arXiv 每次请求间隔；实际至少为 3.2 秒 |
| `--request-delay` | `0.8` | OpenReview 同一主机请求间隔秒数 |
| `--cache-dir` | `.cache/paper-watch` | CVF 缓存与最近一次诊断的目录 |
| `--cvf-cache-hours` | `168` | CVF 缓存有效小时数；`0` 强制重新请求 |
| `--min-score` | `12` | 最低收录分数；无任务证据的论文始终排除 |
| `--new-only` | 关闭 | 仅展示报告日期当天首次入库的论文；定时任务开启 |
| `--report-date` | 当前本地日期 | 日报日期，格式 `YYYY-MM-DD` |

不传 `--new-only` 可查看本次匹配到的全部论文。新/旧以首次入库时间区分，不等同于论文发表日期。`--report-date` 只改变报告日期，不进行历史补抓。

可设置 `PAPER_BOT_USER_AGENT` 提供项目地址和联系方式。`OPENREVIEW_API_BASEURL` 可将 OpenReview 查询限制到一个自定义 API 地址；不设置时使用官方 v2，并在搜索全部失败时回退 v1。

## 输出

```text
papers.db                           去重后的论文、来源、运行记录
reports/YYYY-MM-DD.md                中文日报
reports/YYYY-MM-DD.json              同一日报的结构化数据和来源状态
.cache/paper-watch/cvf-details.json  CVF 详情缓存（不提交 Git）
.cache/paper-watch/last-run.json     最近运行的来源诊断（包括全失败）
```

日报包含作者、来源、论文/PDF/代码链接、日期、关键词、评分依据、摘要、方法线索、与水下增强的关系及 `S/A/B/C` 推荐等级。JSON 包含相同的论文数据，适合后续分析或接入阅读工具。

来源返回成功但没有匹配结果属于正常运行；全部来源不可用属于失败。部分来源不可用时仍生成日报，附上警告。无新增论文不等同于采集失败。

### 去重与已有数据

直接沿用原有 `papers.db`，无需重建数据库。匹配顺序为来源标识、标准化 DOI、标准化标题，最后使用同第一作者姓氏下标题 Jaccard 相似度 `>= 0.90` 的回退匹配。

合并时优先保留 CVF，其次 OpenReview，再次 arXiv 的主来源信息，同时补充其他来源的摘要或代码。首次升级运行会用新规则重算旧库的推荐，保留论文标识、元数据和首次/最近发现时间；不相关条目保留在库中，但分数降为 0。再次采集时使用合并后的元数据评分。升级不会删除历史条目，也不会改写以前的日报。

## GitHub Actions

[每日采集工作流](.github/workflows/collect-cv-papers.yml) 每天 **07:30（Asia/Hong_Kong）** 运行，也可在 Actions 页面手动启动。

工作流先运行离线回归测试，再恢复 CVF 缓存并采集，成功后将数据库与日报提交回运行分支。来源状态显示在 Actions 摘要中；诊断文件作为 artifact 保存 14 天。全部来源失败会让任务失败，并跳过数据库和日报提交。

[测试工作流](.github/workflows/tests.yml) 在代码变更和 PR 时运行，覆盖 Linux 的 Python 3.11/3.13 与 Windows 的 Python 3.11。测试不调用真实外部接口。

```bash
python -m unittest discover -s tests -v
```

## 上游接口

- [arXiv API 手册](https://info.arxiv.org/help/api/user-manual.html)
- [arXiv 官方每日 RSS / Atom 源](https://info.arxiv.org/help/rss.html)
- [CVF Open Access](https://openaccess.thecvf.com/menu)
- [OpenReview API v1 / v2](https://docs.openreview.net/getting-started/using-the-api/installing-and-instantiating-the-python-client)
- [OpenReview 官方搜索接口实现](https://github.com/openreview/openreview-py/blob/master/openreview/api/client.py)
