> 返回 [README](../README.md)

# 接口

本地 HTTP 服务，默认 `http://127.0.0.1:8765`。请求与响应都是 JSON（`Content-Type: application/json`）。

失败时统一返回：

```json
{"ok": false, "error": "说明文字"}
```

HTTP 状态码：400 参数错误，500 内部错误。

---

## 问卷

### GET /api/survey

返回当前加载的问卷结构。

```json
{
  "ok": true,
  "survey": {
    "title": "中小学生研学旅行现状调查问卷",
    "question_count": 17,
    "required_count": 17,
    "source_url": "https://v.wjx.cn/vm/OfJ8WKu.aspx",
    "questions": [
      {
        "topic": 8,
        "type": 3,
        "type_name": "单选",
        "required": true,
        "prompt": "8.实施机构是?",
        "has_option_text": true,
        "option_text_count": 4,
        "options": [
          {"value": "1", "text": "A.学校自行组织", "has_text": true,
           "text_id": "tqq8_1", "text_required": true}
        ]
      }
    ]
  },
  "shortid": "OfJ8WKu",
  "sig": "6827c3c49210"
}
```

`sig` 是问卷指纹（题干 + 题型 + 选项的 MD5），答案与配置都按它绑定。

### POST /api/survey

抓取指定问卷并解析，结果落盘到 `survey.json`。

```json
{"shortid": "OfJ8WKu", "proxy": "http://127.0.0.1:7890"}
```

`shortid` 支持三种形式：纯短链 `OfJ8WKu`、`https://v.wjx.cn/vm/OfJ8WKu.aspx`、带 `#1` 锚点的完整链接。后端会先规范化。

---

## 作答

### POST /api/quick/start

```json
{
  "answers": {"1": "2", "8": "4", "9": ["1", "3"]},
  "option_texts": {"8": {"4": "某某研学基地"}},
  "survey_sig": "6827c3c49210",
  "count": 3,
  "gap": 60,
  "proxy": "",
  "choice_random": false
}
```

| 字段 | 说明 |
|---|---|
| `answers` | `{题号: 值}`。单选是字符串，多选是数组，矩阵是分值数组 |
| `option_texts` | 联动选项的补充文本，`{题号: {选项值: 文本}}`，可省略 |
| `survey_sig` | 答案属于哪份问卷。与当前问卷不符时整份作废 |
| `count` | 轮数，1–200 |
| `gap` | 轮间隔秒数，0–7200 |
| `choice_random` | 是否每轮重抽未配置概率的选择题 |

未出现在 `answers` 里的必答题由后端随机补齐。

### POST /api/quick/stop

停止当前任务，会直接终止浏览器子进程。无请求体。

### GET /api/quick/status

```json
{
  "ok": true,
  "running": true,
  "steps": [
    {"key": "env", "label": "检查环境", "state": "done", "detail": "指纹 safari17_2_ios"},
    {"key": "submit", "label": "提交答卷", "state": "running", "detail": "3/450"}
  ],
  "result": {
    "ok": true,
    "success": 3,
    "joinid": "128153311680",
    "answers": {"1": "2"},
    "option_texts": {"8": {"4": "某某研学基地"}},
    "engine": "browser",
    "sig": "6827c3c49210"
  }
}
```

`result.answers` 是第 1 轮实际提交的内容，运行结束后回填界面。

### GET /api/browser/status

```json
{
  "ok": true,
  "running": true,
  "phase": "filling",
  "current": 1,
  "total": 17,
  "round": 3,
  "rounds": 450,
  "qtext": "Q1 · 您的性别",
  "captcha": false,
  "captcha_mode": ""
}
```

`phase` 取值：`opening` / `filling` / `submitting` / `waiting` / `done` / `idle`。

---

## 选项概率

### GET /api/weights

```json
{
  "ok": true,
  "sig": "6827c3c49210",
  "current_sig": "6827c3c49210",
  "stale": false,
  "rounds": {
    "1": {
      "8": {
        "mode": "multi_target",
        "weights": {"1": 0.7, "2": 0.6, "3": 0.3},
        "count": null,
        "sig": "1d4fd549b6"
      }
    }
  },
  "pairs": 1,
  "configured_rounds": [1],
  "questions": ["8"]
}
```

`stale=true` 表示配置属于另一份问卷，运行时会被忽略。

### POST /api/weights

三种 `action`。

**保存单题**

```json
{
  "action": "set",
  "round": 2,
  "topic": "8",
  "config": {
    "mode": "multi_target",
    "weights": {"1": 0.7, "2": 0.6, "3": 0.3},
    "count": null
  }
}
```

`config` 为空或 `cleared: true` 等于删除该轮该题。

**清除**

```json
{"action": "clear", "round": 2, "topic": "8"}
{"action": "clear", "round": 2}
{"action": "clear", "all": true}
```

**复制**

```json
{"action": "copy", "from": 1, "to": [2, 3]}
{"action": "copy", "from": 1, "to": "all", "count": 5}
{"action": "copy", "from": 1, "topic": "8", "to": "all", "count": 5}
```

带 `topic` 时只复制该题，目标轮次的其他题保持不变。不带 `topic` 是整轮复制，走深拷贝。

所有写操作都会返回完整的配置对象，前端直接用返回值重建列表。

### POST /api/weights/preview

按草稿或已存配置跑蒙特卡洛，对比目标与实际。

```json
{
  "topic": "8",
  "config": {"mode": "multi_target", "weights": {"1": 0.7, "2": 0.6}},
  "samples": 600
}
```

不传 `config` 时按 `round + topic` 读已存配置。

```json
{
  "ok": true,
  "target": {"1": 0.7, "2": 0.6},
  "actual": {"1": 0.697, "2": 0.598},
  "mean_count": 1.99,
  "count_hist": {"1": 1146, "2": 1731, "3": 1123},
  "fit_error": 0.000099,
  "warnings": []
}
```

多选的 `actual` 分母是轮次数，不是勾选格总数。

---

## 模板

### GET /api/fill/template

```json
{
  "ok": true,
  "items": {"4": ["西塘村", "某某中学"]},
  "option_texts": {"8": {"1": ["某某中学"], "4": ["某某基地"]}},
  "random": true,
  "sig": "6827c3c49210",
  "current_sig": "6827c3c49210",
  "stale": false,
  "linked_options": 2
}
```

`items` 是填空题候选，`option_texts` 是联动选项候选。两者都按问卷 `sig` 绑定。

### POST /api/fill/template

```json
{
  "items": {"4": ["西塘村"]},
  "option_texts": {"8": {"4": ["某某基地", "某某机构"]}},
  "random": true
}
```

空数组等于删除该项。`random=true` 时每轮按洗牌队列取候选，`false` 时固定取第一项。

---

## 导出

### GET /api/survey/export

```json
{
  "ok": true,
  "title": "中小学生研学旅行现状调查问卷",
  "shortid": "OfJ8WKu",
  "text": "1.您的性别: [单选题]\nA.男\nB.女\n...",
  "count": 17,
  "warnings": []
}
```

只格式化内存里的问卷，不重新抓页面，避免覆盖正在作答的 `survey.json`。

---

## 日志

### GET /api/batch/status?since=N

增量拉取日志，`since` 是上次拿到的序号。

```json
{"ok": true, "logs": [{"t": "22:20:34", "level": "ok", "text": "第 1/1 轮 成功"}], "seq": 42}
```

---

## HTTP 批量（命令行用）

界面已不再调用，外部脚本仍可用：

| 方法 | 路径 |
|---|---|
| POST | `/api/batch/start` |
| POST | `/api/batch/pause` |
| POST | `/api/batch/stop` |
| GET | `/api/batch/status` |

`/api/batch/start` 请求体：`{answers, count, gap, think, proxy, shortid}`。

---

## 其他

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/about` | 版本、作者、免费声明 |
| GET | `/api/answers/random` | 随机一份完整答案 |
| POST | `/api/answers/preview` | 按答案拼 `submitdata`，不提交 |
