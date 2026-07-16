# 8-K Auto-Drafting — Findings Report & Recommended Architecture

_Project: Law_RAG (Richtech). Date: 2026-07-16. Audience: product owner, RTX training,
legal counsel. This report concludes the findings after five build-and-test iterations
(v1→v5) and states the recommended path. **English first, 中文在后半部分。**_

---

## Executive summary

- **Goal:** feed a contract / news / supplements to the web app → get a correctly formatted,
  standard, accurate 8-K that is as close to publishable as possible; optionally match the
  customer's historical tone.
- **What we learned across five iterations:** every approach that lets the model *generate the
  disclosure and then constrains it* hits the same wall — a generative model cannot be trusted
  with facts, and policing generated facts is an unwinnable game.
- **The decisive reframe:** invert the model's job. **Use the model to UNDERSTAND/EXTRACT
  (low-risk, verifiable against the source); use CODE to GENERATE (zero fabrication).**
- **The remaining gap is an information problem, not a model problem.** The disclosure often
  needs facts/context the single contract does not contain. No model architecture fixes a
  missing input — the answer is better extraction + explicit human-supplied supplements.
- **Recommended production spine:** *Extract → verify → deterministic assemble/hybrid → guardrail
  → human gap-fill.* The fine-tuned adapter (v2) and delex (v5) become **optional** layers, not
  the core. This needs **no further model training**.

---

## The problem, from first principles

An 8-K Item disclosure is four different things, each with a different correct source:

| Component | Example | Correct source | Status |
|---|---|---|---|
| **Structure** | cover page, item heading, qualifier, signature, exhibit index | deterministic template | **Solved** (`export.py`, EDGAR-faithful) |
| **Facts** | parties, dates, amounts, share counts, terms | source doc via extraction + verbatim-quote verify — **never model weights** | The compliance red line |
| **Materiality** | which facts are material enough to disclose | learned rubric / per-Item rules | Built (materiality rubric from 17 real filings) |
| **Narrative/context** | business rationale, forward-looking framing | **often NOT in the contract** → human input | Built (`add_business_context`) |

The hard, recurring problem is **Facts** and **Narrative**: a generative model will invent them,
and much of the Narrative is genuinely not in the input at all.

---

## The five iterations (what each proved)

| Iter | Approach | Result | Finding (the value is the negative result) |
|---|---|---|---|
| **v1** | Qwen 3.6 base + RAG/retrieve/rerank generates the disclosure; facts via RAG extraction + verbatim-quote check; rule-based structure | Works on clean synthetic + some real docs | Model still writes prose; on messy/redacted real docs a citation once quoted a summary, not the source → added `verify_quote`. Facts-via-RAG is sound; prose fluency ≠ fact safety. |
| **v2** | Fine-tune Qwen 3.6 on 2,174 real (source→disclosure) pairs for style/structure | **Clear style win:** ROUGE-L 0.246→0.464, number-recall 0.577→0.675, length 2430→1098 chars | **But FABRICATES figures** on number-dense disclosures (re-invented a whole consideration schedule). Fine-tuning teaches style, **not** fact fidelity. |
| **v3** | Retrain on cleaned data (more/better) | **Did NOT beat v2** (number-recall flat/lower) | **Proves fabrication is structural**, not a data-volume problem. More training will never fix facts. → Decision: v2 is the final *style* adapter. |
| **gate** | Fact-fidelity **guardrail** (RED-only) reconciles every figure vs the source; blocks fabrication | Blocks fabricated numbers end-to-end | A **detector, not a fixer.** Catches wrong numbers; cannot catch a fabricated *non-numeric* narrative, and cannot invent a missing fact. Necessary, not sufficient. |
| **v4/v5** | **Delexicalize**: mask facts with typed placeholders so the model *structurally cannot* emit a real number; backfill from source | v4 deployed; then found **wrong-org slot misalignment** (張冠李戴); fixed numbering + built a groundability filter | **Delex only works for transaction Items (2.03 78%, 3.02 78%); it collapses on the narrative core (1.01 6%, 5.02 0%).** And placeholders get emitted for facts the contract does not contain. |

### Key measurements (2026-07-16)

- **Window is not the bottleneck.** Widening the training window 15k→full 120k raises placeholder
  alignment only **34.9% → 39.9% (+5 pts)**. Median source doc is 17.5k — the facts are not
  beyond the window, they are simply **absent from the paired exhibit**. ⇒ the proposed full-text
  corpus + ZeRO-3 long-context training was **abandoned**.
- **Value-aware canon** (`$38.7M`≡`$38,700,000`, dates→ISO; rounding kept distinct) lifts overall
  alignment 36.3%→40.6% — cheap, and equal to the entire window gain.
- **Groundability filter @0.90:** keep 1,005/2,174 pairs (99.9% mean groundability), but split by
  Item: **2.03/3.02 ≈ 78%, 1.01 = 6%, 5.02 = 0%.** Confirms delex is Item-dependent.

---

## The decisive reframe

All of v1→v5 are one paradigm: **"model generates → we constrain."** It always fights fabrication.

The robust paradigm inverts the roles:

> **Model = comprehension (extract & verify against the source). Code = production (template &
> assemble). The model never authors a fact.**

This is the `assemble` / `hybrid` mode already built. For a **legal-compliance** product,
**reliability ≫ fluency** — which matches the stated priority: *"at least accurate and standard;
tone is a bonus."*

## The core insight

**The remaining gap is an information problem, not a model problem.** Two cases when a required
figure is missing from the contract:

1. **Derivable** (e.g. share count = aggregate ÷ price per share) → compute it deterministically
   (`_derive_share_count`), grounded and shown.
2. **Genuinely absent** (business context, an unstated total) → **OMIT or ASK the human. Never
   invent.** The guardrail RED-flagging it for a human fill is the design *working*, not a bug.

⇒ The highest-ROI levers are **extraction completeness** and a **structured "supplements /
gap-fill" input** — exactly the "contract / news / any other supplements" the product already
envisions. Not more model training.

---

## Options evaluated

| # | Option | Pros | Cons | Verdict |
|---|---|---|---|---|
| 1 | Pure generative + guardrail (v2/v3) | fluent | fabrication whack-a-mole; needs retrains; fact-leakage | ❌ proven fragile |
| 2 | Delex / v5 | structurally no fabricated numbers | only 2.03/3.02; fails 1.01; complex; RTX dependency | ⚠️ narrow/optional |
| 3 | **Extract → assemble/hybrid + guardrail + gap-fill** | accurate, reliable, composes multi-doc, no RTX, maintainable | prose plainer; depends on extraction quality | ✅ **recommended spine** |
| 4 | Option 3 + optional v2 style polish (always behind the guardrail) | fluent **and** safe | keeps adapter dependency | ✅ enhancement of 3 |

## Recommendation

Adopt **Option 3 as the production spine, Option 4 as an enhancement:**

1. **Facts:** base model (not fine-tuned) does structured **extraction** (per-Item checklist →
   JSON + verbatim quote), verified, then deterministic backfill. RAG grounds the extraction.
2. **Structure:** deterministic templates (`export.py`) — 100% reliable; the model is never
   responsible for format.
3. **Prose:** default `hybrid` (model writes 8-K-style prose; guardrail blanks any ungrounded
   number) or `assemble` (fully deterministic). The v2 adapter is an **optional polish layer,
   always behind the guardrail**; the production path need not depend on it.
4. **Missing info = supplements:** guardrail RED items become a **form the user fills**
   (structured fields + free-text business context); the filled values are grounded facts.
5. **Multiple inputs:** contract → 1.01/2.03/3.02; news/press release → 8.01/7.01; supplements →
   fill gaps. The extraction spine merges multiple sources cleanly.
6. **Customer tone:** NOT fine-tuning — retrieve the customer's past same-Item 8-Ks as **few-shot
   style exemplars (facts stripped)**. Too few filings to fine-tune, and fine-tuning re-introduces
   fact leakage.
7. **Delex/v5:** optional, scoped to 2.03/3.02, low priority — revisit only if those Items become
   a business focus.

## Recalibrating "publishable"

Zero-human, direct auto-publish is **not achievable** when the input lacks the information (and
lawyer sign-off is legally required regardless). The achievable, valuable target:

> **Structure/boilerplate 100% done + every grounded fact filled + every ungrounded fact clearly
> flagged for a quick human fill.** The lawyer fills a few flagged fields and signs — instead of
> drafting from scratch. This saves ~80% of the effort **with accuracy guaranteed.**

## Bottom line

The path was **not wrong** — it was five correct experiments, each producing a valuable negative
result, that together converge on the answer. But the answer is **not v5**. It is: **the model
understands, code generates, humans fill the gaps, the guardrail backstops.**

---
---

# 8-K 自动起草 — 结论报告与推荐架构（中文）

_项目：Law_RAG（Richtech）。日期：2026-07-16。读者：产品负责人、RTX 训练方、法律顾问。本报告
总结五次「构建—测试」迭代（v1→v5）的发现，并给出推荐路径。_

## 摘要

- **目标：** 在网页里喂入合同 / 新闻 / 补充材料 → 得到格式正确、标准、准确、尽可能接近可直接
  发布的 8-K；能贴合客户历史 tone 更好。
- **五次迭代的共同教训：** 凡是「让模型生成披露、再去约束它」的做法，都撞同一堵墙——生成式模型
  不能被信任来承载事实，而事后管住生成的事实是一场打不赢的仗。
- **决定性的重构：** 把模型职责反过来。**模型用来「理解/抽取」（低风险、可对原文校验）；代码
  用来「生成」（零编造）。**
- **剩下的差距是信息问题，不是模型问题。** 披露常需要单份合同里没有的事实/背景；没有任何模型
  架构能补上缺失的输入——正解是更好的抽取 + 显式的人工补录。
- **推荐的生产主干：** *抽取 → 校验 → 确定性拼装/hybrid → 护栏 → 人工补缺。* 微调适配器（v2）
  与 delex（v5）降为**可选**层，而非核心。此路线**无需再训练模型**。

## 从第一性原理看问题

一份 8-K Item 披露其实是四种东西，各有各的正确来源：

| 组成 | 例子 | 正确来源 | 状态 |
|---|---|---|---|
| **结构** | 封面、Item 标题、qualifier、签名、附件索引 | 确定性模板 | **已解决**（`export.py`，EDGAR 级）|
| **事实** | 当事方、日期、金额、股数、条款 | 源文档抽取 + 逐字引用校验——**绝不来自模型权重** | 合规红线 |
| **材料性** | 哪些事实重大到要披露 | 学到的 rubric / 各 Item 规则 | 已建（从 17 份真实 filing 得出）|
| **叙事/背景** | 业务动机、前瞻性表述 | **常常不在合同里** → 人工输入 | 已建（`add_business_context`）|

真正反复出问题的是**事实**和**叙事**：生成式模型会去发明它们，而叙事很多时候压根不在输入中。

## 五次迭代（各自证明了什么）

| 迭代 | 做法 | 结果 | 发现（价值在于否定结论）|
|---|---|---|---|
| **v1** | Qwen 3.6 base + RAG/检索/重排生成披露；事实走 RAG 抽取 + 逐字引用校验；规则管结构 | 在干净合成 + 部分真实文档上可用 | 模型仍在写 prose；在脏/涂黑的真实文档上，引用曾引到摘要而非原文 → 加了 `verify_quote`。事实走 RAG 是对的；流畅 ≠ 事实安全。|
| **v2** | 在 2,174 对真实（源→披露）上微调 Qwen 3.6 学风格/结构 | **风格明显提升：** ROUGE-L 0.246→0.464，数字召回 0.577→0.675，长度 2430→1098 字符 | **但在数字密集披露上编造数字**（重新发明整套对价结构）。微调教得会风格，**教不会**事实保真。|
| **v3** | 用更干净/更多数据重训 | **没打赢 v2**（数字召回持平/更低）| **证明编造是结构性的**，不是数据量问题。再训也修不好事实。→ 定 v2 为最终*风格*适配器。|
| **gate** | 事实保真**护栏**（仅 RED）逐一核对每个数字 vs 源文档，拦截编造 | 端到端拦住编造数字 | 是**探测器，不是修复器。** 能抓错数字；抓不住非数字的叙事编造，也无法凭空补出缺失事实。必要但不充分。|
| **v4/v5** | **去词化（delex）**：用类型化占位符遮盖事实，使模型*结构上无法*产出真实数字；再从源文档回填 | v4 部署；发现**机构槽位张冠李戴**；修好编号 + 建了可对齐度过滤器 | **delex 只对交易类 Item 成立（2.03 78%、3.02 78%），对叙事型核心崩塌（1.01 6%、5.02 0%）。** 且会为合同里没有的事实吐出占位符。|

### 关键测量（2026-07-16）

- **窗口不是瓶颈。** 训练窗口从 15k 拉到全文 120k，占位符对齐率只从 **34.9% → 39.9%（+5 分）**。
  中位源文档才 17.5k——事实不在窗口之外，而是**不在这份配对文档里**。⇒ 全文语料 + ZeRO-3 长上下文
  训练方案**已放弃**。
- **值感知 canon**（`$38.7M`≡`$38,700,000`、日期→ISO，四舍五入保持独立）把整体对齐率从 36.3% 提到
  40.6%——便宜，且相当于整个窗口的收益。
- **可对齐度过滤 @0.90：** 保留 1,005/2,174 对（平均可对齐度 99.9%），但按 Item 分：**2.03/3.02 约
  78%，1.01 = 6%，5.02 = 0%。** 坐实 delex 是分 Item 的。

## 决定性的重构

v1→v5 是同一种范式：**「模型生成 → 我们约束」**，永远在跟编造搏斗。稳健的范式把角色反过来：

> **模型 = 理解（抽取并对源文档校验）。代码 = 生产（模板与拼装）。模型永不撰写事实。**

这就是已经建好的 `assemble`/`hybrid` 模式。对一个**法律合规**产品，**可靠性 ≫ 流畅度**——恰好
符合既定优先级：*「至少要准确、标准；tone 是加分」*。

## 核心洞见

**剩下的差距是信息问题，不是模型问题。** 合同缺某个必需数字时有两种情况：

1. **可推导**（如股数 = 总额 ÷ 每股价）→ 确定性计算（`_derive_share_count`），接地并展示。
2. **确实缺失**（业务背景、未写明的总数）→ **省略或询问人工，绝不发明。** 护栏把它标红交人工填，
   正是设计在*正常工作*，不是 bug。

⇒ ROI 最高的杠杆是**抽取完整度**和一个结构化的**「补充/补缺」输入**——正是产品设想的「合同 / 新闻 /
其他补充材料」。而不是再训练模型。

## 评估的选项

| # | 选项 | 优点 | 缺点 | 判断 |
|---|---|---|---|---|
| 1 | 纯生成 + 护栏（v2/v3）| 流畅 | 编造打地鼠；要反复重训；事实泄漏 | ❌ 已证明脆弱 |
| 2 | delex / v5 | 结构上不产出假数字 | 只行于 2.03/3.02；1.01 失败；复杂；依赖 RTX | ⚠️ 窄用/可选 |
| 3 | **抽取 → 拼装/hybrid + 护栏 + 补缺** | 准确、可靠、支持多文档合并、不依赖 RTX、易维护 | prose 较朴素；吃抽取质量 | ✅ **推荐主干** |
| 4 | 选项 3 + 可选 v2 风格润色（永远在护栏之后）| 既流畅**又**安全 | 保留适配器依赖 | ✅ 选项 3 的增强 |

## 推荐

采用**选项 3 为生产主干，选项 4 为增强：**

1. **事实：** base 模型（非微调）做结构化**抽取**（各 Item checklist → JSON + 逐字引用），校验后
   确定性回填。RAG 用于抽取接地。
2. **结构：** 确定性模板（`export.py`）——100% 可靠；模型永不负责格式。
3. **prose：** 默认 `hybrid`（模型写 8-K 风格、护栏挖空未接地数字）或 `assemble`（全确定性）。
   v2 适配器为**可选润色层，永远在护栏之后**；生产主路可不依赖它。
4. **缺失信息 = 补充材料：** 护栏标红项变成**让用户填的表单**（结构化字段 + 自由文本业务背景）；
   填入的值即接地事实。
5. **多输入：** 合同 → 1.01/2.03/3.02；新闻稿 → 8.01/7.01；补充材料 → 填空。抽取式主干天然合并多源。
6. **客户 tone：** **不是微调**——检索客户历史同类 Item 8-K 作为**few-shot 风格样例（剥离事实）**。
   filing 太少不宜微调，且微调会重新引入事实泄漏。
7. **delex/v5：** 可选，限定 2.03/3.02，低优先级——仅当这些 Item 成为业务重点再回头。

## 重新校准「可发布」

在输入缺信息时，「零人工、直接自动发布」**做不到**（何况律师签字本就是法律要求）。可达成且有价值的目标：

> **结构/boilerplate 100% 完成 + 每个接地事实已填 + 每个未接地事实清楚标红待人工快速补录。**
> 律师填几个红格并签字，而非从零起草。省掉约 80% 的工作量，且**准确性有保证。**

## 结论

路径**没有错**——是五次正确的实验，各自得出有价值的否定结论，共同收敛到答案。但答案**不是 v5**，
而是：**模型理解、代码生成、人工补缺、护栏兜底。**
