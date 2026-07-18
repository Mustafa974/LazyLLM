# Patch 重构实施方案

## 一、目标与边界

本次重构只解决单种格式下单个修改任务的完整运行问题，不搭建多任务、多轮次、跨格式统计的实验框架。

核心目标是让 LLM 输出一种简洁、明确、直接作用于 `WriterDocument` 的统一 `PatchSet`，并让 Apply 层只负责校验和执行 Patch，不在 Patch 之外推导修改内容。

本次范围包括：

- JSON、XML、Markdown 三条路线继续使用同一个 `PatchSet`；
- 支持局部文本替换、Block 插入、Block 删除和 Block 移动；
- 保留未修改文本的原始样式；
- 本地 Apply 必须原子化，失败时不得返回部分修改结果；
- 本地 Apply 成功后才允许写回飞书。

本次暂缓：

- 多个任务的批量实验和统计；
- 任务 9 的长文档上下文裁剪；
- 任务 10 的 Outline、Draft、Final 三阶段生成；
- URL、真实链接目标和平台交叉引用；
- stage 在模型投影和 Patch 中的编辑；
- 新建复杂嵌套 Block 和新建复杂富文本内容。

XML 和 Markdown 投影的主体结构不在本次重写，只调整与新 Patch 相关的 prompt 和 Adapter 行为。

---

## 二、设计原则

### 2.1 保留统一 Patch 结构

继续使用现有的：

```text
PatchSet
    └── PatchHunk[]
            └── PatchBlock[]
```

不为 `replace / insert / delete / move` 分别创建独立的 Patch 类型。`PatchHunk.modify_type` 负责区分操作，不增加复杂的操作类型联合。

### 2.2 Patch 必须完整表达修改

LLM 决定：

- 修改哪个 Block；
- 修改 Block 中的哪个字符范围；
- 替换成什么文字；
- 插入哪些新 Block；
- 删除或移动哪些 Block；
- 移动到哪个锚点之前或之后。

Apply 层不得：

- 根据标题猜测章节结束位置；
- 自动扩大删除或移动范围；
- 自动修改编号；
- 根据全文 diff 猜测 LLM 修改了哪一段；
- 在 Patch 之外产生额外正文修改。

### 2.3 确定性逻辑只实现操作语义

Apply 层可以进行以下确定性工作：

- 校验 Block ID、文本范围和移动锚点；
- 检查文本范围是否重叠；
- 保留文本范围之外的原始 Span；
- 生成新 Block 的本地 ID；
- 按 Patch 顺序执行插入、删除和移动；
- 在执行前完成完整预校验。

这些逻辑只执行 Patch 已经声明的内容，不替代 LLM 做语义决策。

---

## 三、Patch 类型

### 3.1 PatchHunk

保留一个 `PatchHunk` 类型：

```python
class PatchHunk(BaseModel):
    target_node_id: str
    modify_type: Literal['replace', 'insert', 'delete', 'move']

    anchor_node_id: Optional[str] = None
    text_range: Optional[Tuple[int, int]] = None
    old_text: Optional[str] = None
    new_text: Optional[str] = None
    new_blocks: List[PatchBlock] = Field(default_factory=list)
    position: Optional[Literal['before', 'after']] = None
```

删除现有字段：

- `include_section`；
- `preserve_styles`；
- `PatchHunk.meta`；
- `inside_start / inside_end`。

`old_text` 继续存在，但不由 LLM 输出。Adapter 在模型返回 Patch 后，根据原始 `WriterDocument` 和 `text_range` 填充它，用于 Apply 和写回前的一致性校验。

### 3.2 PatchBlock

保留已经引入的 `PatchBlock`，但收缩到当前任务需要的字段：

```python
class PatchBlock(BaseModel):
    type: Literal['paragraph', 'heading', 'list_item', 'code', 'quote']
    content: str = ''
    numbering: Dict[str, Any] = Field(default_factory=dict)
```

删除现有字段：

- `node_id`：由系统生成，LLM 不得指定；
- `spans`：当前插入任务只要求普通文本；
- `children`：任务 10 暂缓后，当前没有新建嵌套子树的要求。

以后确实需要生成复杂富文本或嵌套 Block 时，再基于真实任务扩展，不在本次提前设计。

### 3.3 删除 Anchor

`Anchor` 如果只保存一个 `node_id`，没有独立类型的价值，因此直接删除。移动操作的目标锚点由 `PatchHunk.anchor_node_id` 表达，并与 `ModifyInstruction.anchor_node_id` 保持一致。

文本位置统一由 `PatchHunk.text_range` 表达，原有 `heading_path` 和 `text_offset` 一并删除。

---

## 四、四种操作的严格语义

### 4.1 replace

必要字段：

```text
target_node_id
text_range = [start, end)
new_text
```

约束：

- `text_range` 使用 `WriterBlock.content` 的 Python Unicode 字符偏移；
- `0 <= start <= end <= len(content)`；
- `new_text` 只包含替换范围的新文字，不包含完整 Block 内容；
- Adapter 将 `content[start:end]` 填入 `old_text`；
- Apply 校验当前范围内容仍等于 `old_text` 后才执行。

样式处理采用固定规则：

- 范围之外的 Span 原样保留；
- 替换范围完全位于同一个 Span 内时，新文字继承该 Span 的样式；
- 替换范围跨越多个不同样式时，新文字使用默认样式；
- 不使用 `SequenceMatcher`，不根据完整新旧文本猜测对应关系。

同一 Block 存在多个 replace hunk 时：

- 所有 `text_range` 都基于原始 Block 内容；
- 范围不得重叠；
- Apply 按 `start` 从大到小执行，避免前一次替换改变后续偏移。

### 4.2 insert

必要字段：

```text
target_node_id
position = before | after
new_blocks
```

语义：

- `target_node_id` 是现有锚点 Block；
- `new_blocks` 按列表顺序插入；
- 新 Block ID 由 Apply 层生成；
- 不再支持使用 `new_text` 隐式创建普通段落。

插入一个普通段落也必须明确输出：

```json
{
  "modify_type": "insert",
  "target_node_id": "anchor-id",
  "position": "after",
  "new_blocks": [
    {"type": "paragraph", "content": "新段落", "numbering": {}}
  ]
}
```

### 4.3 delete

必要字段：

```text
target_node_id
```

每个 delete hunk 只删除一个 `WriterBlock`。如果该 Block 本身拥有 `children`，其真实子树随 Block 一起删除。

如果一个逻辑章节在核心 DocIR 中由多个平级 Block 组成，LLM 必须为每个 Block 输出一个 delete hunk。Apply 不根据标题层级自动推导章节范围。

### 4.4 move

必要字段：

```text
target_node_id
anchor_node_id
position = before | after
```

每个 move hunk 只移动一个 `WriterBlock`。如果该 Block 拥有 `children`，真实子树随 Block 一起移动。

对于由多个平级 Block 组成的逻辑章节，LLM 输出多个 move hunk。PatchSet 的顺序决定最终顺序：

- 多个 Block 移到同一锚点之前时，按源 Block 顺序输出；
- 多个 Block 移到同一锚点之后时，按源 Block 逆序输出；
- 也可以让后续 Block 锚定到前一个已移动 Block，以更直观地表达最终顺序。

Apply 不使用 `_section_end` 或 `include_section` 扩大移动范围。

---

## 五、ModifyPlan 与 Patch 生成

`ModifyPlan` 继续保留，不在本次改成另一套规划类型。

Plan prompt 需要明确：

- 一个 PatchHunk 只修改一个 Block；
- 逻辑章节由多个平级 Block 组成时，Plan 必须列出所有 Block；
- 编号文字发生变化时，必须为对应标题增加明确的 replace instruction；
- Apply 不会自动重排编号或修改引用文本。

Patch prompt 需要明确：

- replace 输出字符范围和局部新文字；
- 为 replace 目标提供三条路线一致的 Unicode 字符索引表；
- 不输出完整 Block 新内容；
- 不输出 `old_text`；
- insert 统一输出 `new_blocks`；
- delete 和 move 一次只处理一个 `target_node_id`；
- 不使用 `include_section` 表达隐式范围。

Adapter 只负责：

1. 校验 `target_doc_id`；
2. 校验所有 Block ID 和锚点存在；
3. 校验 replace 的 `text_range`；
4. 从原始 `WriterDocument` 填充 replace 和 delete 的 `old_text`；
5. 写入路线标识。

Adapter 不修正 LLM 选择的目标，不自动补充 Block，也不自动合并操作。

---

## 六、Apply 设计

### 6.1 完整预校验

执行任何操作前先校验整个 PatchSet：

- 所有目标 Block 和锚点存在；
- replace 范围合法且同一 Block 内不重叠；
- insert 的 `new_blocks` 非空；
- move 的目标不是锚点，也不是锚点的祖先；
- delete 或 move 不会导致后续 hunk 的目标无效；
- Patch 目标文档与当前文档一致。

### 6.2 原子化应用

Apply 始终在 `WriterDocument` 深拷贝上运行：

- 预校验失败时不执行任何 hunk；
- 执行中出现异常时丢弃修改副本；
- 只有全部 hunk 成功才返回修改后的文档；
- 失败时返回未修改文档和失败的 `PatchResult`；
- 本地 Apply 失败时，调用方不得继续写回飞书。

### 6.3 样式切分

replace 不再进行全文 diff，而是根据字符范围直接切分原有 Span：

```text
原始 Span
    → range 前缀
    → replacement
    → range 后缀
```

相邻且样式相同的 Span 可以在结果中合并。该过程只保持已有样式，不创造新的样式语义。

---

## 七、飞书写回

写回前必须满足：

1. 本地 Apply 成功；
2. 重新读取远端目标 Block；
3. replace 和 delete 的目标内容仍与 Adapter 填充的 `old_text` 一致；
4. 所有目标和锚点仍然存在；
5. 所有操作均满足当前飞书接口能力。

写回继续逐 hunk 执行，但在开始前完成完整预校验，尽量避免执行到一半才发现后续操作无效。

飞书接口不提供完整事务能力，因此远端调用仍可能出现部分成功。该问题应在 `PatchResult` 中明确记录，但不通过额外的删除重建或静默降级掩盖。

---

## 八、任务覆盖

| 任务 | 当前表达方式 | 状态 |
|---|---|---|
| 1. 唯一文本局部替换 | 单个 replace hunk | 支持 |
| 2. 重复文本精确定位 | Block ID + text_range | 支持 |
| 3. 指定 Block 前后插入段落 | insert + PatchBlock | 支持 |
| 4. 删除 Block | 单个 delete hunk | 支持 |
| 5. 移动 Block 或子树 | 一个或多个 move hunk | 支持 |
| 6. 插入章节并重排可见编号 | insert + 明确的 replace hunks | 支持 |
| 7. 修改标题和正文文字引用 | 多个 replace hunks | 支持文本引用；URL 暂缓 |
| 8. 修改部分富文本并保留样式 | 范围 replace + Span 切分 | 支持 |
| 9. 长文档目录和片段裁剪 | 不属于 Patch 能力 | 暂缓 |
| 10. Outline → Draft → Final | 超出本次单次修改流程 | 暂缓 |

平台原生有序列表编号由平台维护。如果编号是标题正文中的“一、”“1.”等可见文字，必须由 LLM 输出明确的 replace hunk。

---

## 九、代码改动

### 9.1 `writer_ir.py`

- 保留 `PatchSet / PatchHunk / PatchBlock`；
- 为 `PatchHunk` 增加 `text_range`；
- 删除 `Anchor`，移动锚点直接使用 `anchor_node_id`；
- 收缩 `PatchBlock`；
- 删除 `include_section / preserve_styles / inside_start / inside_end`；
- 更新每种操作的 Pydantic 校验。

### 9.2 `model_adapters.py`

- 按 `text_range` 填充 replace 的 `old_text`；
- delete 的 `old_text` 继续从原始 Block 填充；
- 校验锚点和范围；
- 不修改 LLM 生成的操作范围和目标。

### 9.3 `experiment_tools.py`

删除：

- `_ContextSelection`；
- `_select_model_context`；
- `_outline_document`；
- `_document_subset`；
- `generate_staged_document_json/xml/markdown`；
- `_generate_staged_document`；
- `target_stage` 传递和 Apply 逻辑；
- `_section_end`；
- `_remote_section_end`；
- `SequenceMatcher` 样式推导；
- `include_section` 相关本地和写回逻辑。

重写：

- Patch prompt；
- Patch 完整预校验；
- 基于字符范围的 replace；
- 原子化 `apply_patch`；
- 飞书写回前预校验；
- insert、delete、move 的简化分支。

### 9.4 `test.py`

- 继续使用文件开头的真实模型和飞书配置；
- `REVISION_QUERY` 一次只保留一个任务；
- 一次命令只运行一种格式；
- 不增加小函数级冗余测试；
- 使用包含标题、列表和混合样式的真实文档完成端到端验证；
- 本地 Apply 失败时不执行写回。

### 9.5 文档

- 更新整体架构文档中的 Patch 字段；
- 删除自动章节推导、自动编号和 staged generation 的现行描述；
- XML/Markdown 结构文档只更新 Patch 接口部分，不重写投影格式。

---

## 十、实施顺序

1. 修改 `writer_ir.py`，确定最终 Patch schema；
2. 修改 Adapter 的范围校验和 `old_text` 填充；
3. 重写本地原子化 Apply；
4. 修改 Plan/Patch prompt；
5. 修改飞书写回；
6. 删除 context 和 staged generation 代码；
7. 更新单任务真实 LLM 运行入口；
8. 更新相关设计文档；
9. 使用一种格式和一个真实任务完成端到端验收。

本次实施以代码净缩减为目标。新增代码必须直接服务于上述四种 Patch 操作，不引入任务专属分支或未来能力的预留实现。
