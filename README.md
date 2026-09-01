# 城投评级报告爬虫（中国货币网 + 中国债券信息网）

按发行人全称，从银行间市场两个披露平台抓取信用评级报告 PDF，并汇总成文件清单。

两个平台各管一摊，类似沪深交易所：

| 平台 | 网址 | 典型券种 |
|---|---|---|
| 中国货币网 chinamoney | https://www.chinamoney.com.cn/chinese/pjgg/ | 短融、中票、金融债等（交易商协会 / 外汇交易中心） |
| 中国债券信息网 chinabond | https://www.chinabond.com.cn/xxpl/ywzc_fxyfxdh/fxyfxdh_zqzl/ | 企业债等（中央结算公司） |

名单默认用仓库根目录的 `A09_lgfv_issuer_name_only.xlsx`（`issuer_name_clean` 列，4152 家城投）。

## 协议方案（无浏览器）

最终运行不依赖 Playwright / Camoufox。TLS 用 `curl_cffi` 模拟 Chrome。

### 中国货币网

1. `GET /chinese/pjgg/` 拿负载均衡 Cookie `AlteonP10`
2. `POST /dqs/rest/cm-u-rbt/apply`，`key` 为页面 JS 里 `bbb` 的倒序（静态，不是动态签名）
3. `POST /ags/ms/cm-u-notice-issue/ratingAnNotice` 查债项 / 主体 / 重点关注
4. `GET /dqs/cm-s-notice-query/fileDownLoad.do?contentId=...&priority=0&mode=save` 下 PDF

站点前置有电信 CDN。`pageSize` 过大或连打会空 body 的 **403**，客户端会重建会话并退避重试。

### 中国债券信息网

1. `GET /xxpl/ywzc_fxyfxdh/fxyfxdh_zqzl/`
2. `POST /cbiw/trs/getContentByConditions`（JSON，栏目 `评级文件`）
3. 附件地址 = 详情页目录 + `appendixIds` 里的 `P0....pdf`

`common.js` 里有 AES-ECB（`rklnavQccKhKkyVV`），只用于登录态，公开信披列表不签名。

## 安装

```powershell
python -m pip install -r requirements.txt
```

## 用法

单个企业（`-u` 避免 Windows 下日志缓冲）：

```powershell
python -u main.py --issuer 临泉县交通建设投资有限责任公司
```

从 Excel 分批：

```powershell
python -u main.py --excel A09_lgfv_issuer_name_only.xlsx --start 1 --limit 50
```

断点在 `output/progress.json`：记录每家、每个栏目的接口总数、当前页、已列出/已下载条数。默认续跑。

```powershell
python -u main.py --excel A09_lgfv_issuer_name_only.xlsx
```

关掉代理或加大下载并发：

```powershell
python -u main.py --issuer 临泉县交通建设投资有限责任公司 --no-proxy --workers 8
```

## 产出

```
downloads/
  chinamoney/<发行人>/<日期>_<评级公司>_<标题>_<id>.pdf
  chinabond/<发行人>/...
output/
  inventory.xlsx   # 总清单
  inventory.csv
  records.jsonl    # 逐条落盘，中断不丢
  summary.csv
  state.json       # 已完成发行人
```

同一份 PDF 在两边都会出现时，`inventory.xlsx` 的 `duplicate_of` 按 sha256 标记。

## 速度与代理

默认 `delay_seconds=0`，下载并发 `workers=12`。动态代理从 58ip 提取，**单次最多 50 条**，失效轮换，池空再提。日期窗口为 `1990-01-01` 至今（接口能接受的最早），一次查全量，不再拆 3 年窗口。

需要登录的中债非公开稿只记 `locked`，不下载、不重试。

重复：`is_duplicate=1`，`dup_reason` 为 `same_file`（sha256）或 `same_title_date`，`duplicate_of` 指向先出现的 content_id。两边文件都保留。
