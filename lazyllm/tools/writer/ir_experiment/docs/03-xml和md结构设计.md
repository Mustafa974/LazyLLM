# XML 和 Markdown 投影结构设计

本文档定义 `WriterDocument` 向 XML 和 Markdown 的两种模型输入投影。两种投影只改变 LLM 阅读文档的表现形式，不改变修改结果的语义：三条路线的 LLM 都生成 `writer_ir.py` 中定义的同一种 `PatchSet`，`PatchSet` 始终直接作用于核心 `WriterDocument`。

---

## 一、设计边界

### 1.1 统一流程

```text
WriterDocument
    ├── XmlDocumentProjection      → XML 文本      ──┐
    └── MarkdownDocumentProjection → Markdown 文本 ─├→ LLM → PatchSet
JSON 恒等投影                                      ──┘
                                                              ↓
                                                    apply_patch(WriterDocument)
```

XML 和 Markdown 不定义各自的 Patch 格式，也不把 Patch 应用于投影对象。LLM 输出的 `target_doc_id` 和 `target_node_id` 仍然使用核心 DocIR 中的 `document_id` 和 `WriterBlock.node_id`。

### 1.2 可编辑语义

投影需要向 LLM 保留下列信息：

- 文档身份和标题上下文；
- Block 的 `node_id`、顺序和层级关系；
- Block 的文档语义，例如段落、标题、列表项、引用和代码块；
- 文本内容以及当前路线能表达的行内样式。

`provider_binding`、`provider_payload`、`source_refs` 等平台运行时信息不进入模型投影。它们仍然保留在原始 `WriterDocument` 中，因为 Patch 最终作用于该原始对象，不需要通过投影往返保存这些字段。

### 1.3 不采用字段化复制

XML 和 Markdown 投影不复制 JSON 路线的 `type / content / spans / children` 字段结构，而是使用各自的原生文档语法表达同样的语义：

```text
WriterBlock(type='heading', numbering={'level': 2})
    XML      → <h2>...</h2>
    Markdown → ## ...

WriterSpan(style=['bold'])
    XML      → <b>...</b>
    Markdown → **...**
```

`content` 由文本节点拼接得到，`spans` 由行内语法解析得到，不在投影中重复存储。

---

## 二、XML 投影

XML 投影参考 `docs/lark-doc/references/lark-doc-xml.md` 的 HTML 子集设计，使用块级标签表达 Block 类型，使用行内标签表达 `WriterSpan.style`。

### 2.1 类型

XML 路线只定义两个自定义类型。

#### `XmlBlockProjection`

```python
class XmlBlockProjection:
    element: Element

    @property
    def node_id(self) -> str: ...

    def __str__(self) -> str: ...
```

`element` 直接使用标准 XML 库的 `Element`，不再自定义 XML DOM 类型。`XmlBlockProjection` 的职责是标识哪些 XML 元素对应可被 Patch 寻址的 `WriterBlock`，并从元素的 `id` 属性读取 `node_id`。

它不额外保存 `type`、`content`、`spans` 和 `children`：

- `type` 由 `p / h1-h9 / li / blockquote / pre / table / td` 等标签决定；
- `content` 由元素中的文本节点按顺序拼接得到；
- `spans` 由 `b / em / u / del / code / a / span` 等行内标签解析得到；
- `children` 由 XML 嵌套关系得到。

#### `XmlDocumentProjection`

```python
class XmlDocumentProjection:
    root: Element

    @classmethod
    def from_writer_document(cls, document: WriterDocument) -> XmlDocumentProjection: ...

    def to_writer_document(self) -> WriterDocument: ...

    @classmethod
    def parse(cls, text: str) -> XmlDocumentProjection: ...

    def __str__(self) -> str: ...
```

`XmlDocumentProjection` 持有完整 XML 根节点，负责核心 DocIR 转换、XML 解析和完整文本序列化。

### 2.2 基本格式

```xml
<document id="doc-1">
  <title>文档标题</title>
  <h2 id="b1"><b>重要</b>标题</h2>
  <p id="b2">普通段落</p>
  <ul>
    <li id="b3">第一项</li>
    <li id="b4">第二项</li>
  </ul>
  <blockquote id="b5">引用内容</blockquote>
  <pre id="b6" lang="python"><code>print('hello')</code></pre>
</document>
```

`document`、`ul`、`ol` 等元素可以是投影为了表达文档结构而引入的容器，不必对应 `WriterBlock`。带有 `id` 且属于块级标签的元素才是 `PatchSet.target_node_id` 可寻址的 Block。

### 2.3 行内样式

XML 可以表达当前 `WriterSpan.style` 中的基本文本样式：

| `WriterSpan.style` | XML |
|---|---|
| `bold` | `<b>...</b>` |
| `italic` | `<em>...</em>` |
| `underline` | `<u>...</u>` |
| `strikethrough` | `<del>...</del>` |
| `inline_code` | `<code>...</code>` |
| `link` | `<a>...</a>` |

样式叠加时按固定顺序嵌套。反向解析时维护当前激活的样式栈，每段文本按样式栈生成 `WriterSpan`，最后将所有 `WriterSpan.text` 拼接为 `WriterBlock.content`。

### 2.4 Block 转换

| `WriterBlock.type` | XML |
|---|---|
| `paragraph` | `<p id="...">...</p>` |
| `heading` | `<h1-h9 id="...">...</h1-h9>` |
| `list_item` | `<ul/ol><li id="...">...</li></ul/ol>` |
| `code` | `<pre id="..."><code>...</code></pre>` |
| `quote` | `<blockquote id="...">...</blockquote>` |
| `table` | `<table id="...">...</table>` |
| `table_cell` | `<td id="...">...</td>` |

标题级别从 `numbering['level']` 读取。列表容器是投影节点，连续同类列表项分组到同一个 `ul` 或 `ol` 中。

### 2.5 往返等价

XML 路线的目标是可编辑语义完整往返：

```text
xml_doc = XmlDocumentProjection.from_writer_document(writer_doc)
restored = XmlDocumentProjection.parse(str(xml_doc)).to_writer_document()

editable_semantics(restored) == editable_semantics(writer_doc)
```

XML 文本的转义和反转义统一交给标准 XML 库，不手写字符串替换。

---

## 三、Markdown 投影

Markdown 投影只使用 Markdown 原生语法表达文档内容，不嵌入 XML 标签扩展表达能力。HTML 注释仅用于补充 Markdown 原生语法不具备的文档和 Block 身份。

### 3.1 类型

Markdown 路线同样只定义两个自定义类型。

#### `MarkdownBlockProjection`

```python
class MarkdownBlockProjection:
    node_id: str
    ast_node: MarkdownAstNode

    def __str__(self) -> str: ...
```

`ast_node` 使用 Markdown 解析器产生的原生 AST 节点。标题、列表、引用、代码块以及加粗、斜体等行内样式都由 Markdown 语法和 AST 表达，不定义 `MarkdownSpan`、`MarkdownStyle`、`MarkdownHeading` 等平行类型。

`node_id` 不属于 Markdown 内容，序列化时使用 HTML 注释写入。

#### `MarkdownDocumentProjection`

```python
class MarkdownDocumentProjection:
    document_id: str
    title: str
    blocks: list[MarkdownBlockProjection]

    @classmethod
    def from_writer_document(cls, document: WriterDocument) -> MarkdownDocumentProjection: ...

    def to_writer_document(self) -> WriterDocument: ...

    @classmethod
    def parse(cls, text: str) -> MarkdownDocumentProjection: ...

    def __str__(self) -> str: ...
```

`MarkdownDocumentProjection` 负责文档级注释、Block 顺序、Markdown AST 解析、核心 DocIR 转换和完整序列化。

### 3.2 基本格式

````markdown
<!-- writer:document id="doc-1" title="文档标题" -->

<!-- id="b1" -->
## **重要**标题

<!-- id="b2" -->
普通段落

<!-- id="b3" -->
> 引用内容

<!-- id="b4" -->
```python
print('hello')
```
````

Block 注释是身份锚点，紧邻其对应的 Markdown block。Block 边界、类型和内部样式仍由 Markdown 解析器决定，不在注释中重复声明。

### 3.3 行内样式

| `WriterSpan.style` | Markdown |
|---|---|
| `bold` | `**...**` |
| `italic` | `*...*` |
| `strikethrough` | `~~...~~` |
| `inline_code` | `` `...` `` |
| `underline` | 无原生表示，输出普通文本 |

Markdown 无法表达的样式不报错，也不使用 XML 或自定义标签补齐，而是静默回退到默认样式，仅保留文本。例如：

```text
WriterSpan(text='重要', style=['underline'])
    → Markdown: 重要
```

当一个 Span 同时包含可表达与不可表达样式时，只忽略不可表达部分：

```text
WriterSpan(text='重要', style=['bold', 'underline'])
    → Markdown: **重要**
```

该回退仅针对 Markdown 原生语法无法表达的展示样式，不影响文本内容、Block 身份和文档顺序。

### 3.4 Block 转换

| `WriterBlock.type` | Markdown |
|---|---|
| `paragraph` | 普通段落 |
| `heading` | `#` 至 `######`；7–9 级静默回退为普通段落 |
| `list_item` | `- item` 或 `1. item` |
| `code` | fenced code block |
| `quote` | `> quote` |
| `table` / `table_cell` | GFM table |

Markdown 格式本身负责表达 Block 类型，注释只保存 `node_id`。不把 `type="heading"` 或 `level="2"` 重复写入注释。

### 3.5 Block ID 注释

顶层段落、标题、引用和代码块使用独立前置注释：

```markdown
<!-- id="b1" -->
## 标题
```

列表项不在项目符号之间插入独立注释行，避免破坏列表连续性；注释紧跟在列表项内容之后：

```markdown
- 第一项<!-- id="b2" -->
    - 子项<!-- id="b3" -->
```

表格块的注释放在表格前。表格单元格的 `node_id` 如果需要独立寻址，可以使用单元格内联注释；该语法在实现前需要用最终选定的 Markdown 解析器验证，确认不会改变表格单元格的文本语义。

### 3.6 Markdown 归一化等价

由于 Markdown 对不支持的样式会静默回退到默认样式，Markdown 路线的往返条件是归一化后的可编辑语义等价：

```text
markdown_doc = MarkdownDocumentProjection.from_writer_document(writer_doc)
restored = MarkdownDocumentProjection.parse(str(markdown_doc)).to_writer_document()

editable_semantics(restored)
    == editable_semantics(normalize_for_markdown(writer_doc))
```

`normalize_for_markdown` 只移除 Markdown 无法表达的 Span 样式，不改变文本、Block ID、Block 顺序和可表达的文档结构。它是语义说明，不要为此额外引入独立的投影类型。

---

## 四、与 Adapter 的关系

`XmlAdapter` 和 `MarkdownAdapter` 只负责把核心 DocIR 转换为对应投影文本：

```python
class XmlAdapter:
    def document_to_model_input(self, document: WriterDocument) -> str:
        return str(XmlDocumentProjection.from_writer_document(document))


class MarkdownAdapter:
    def document_to_model_input(self, document: WriterDocument) -> str:
        return str(MarkdownDocumentProjection.from_writer_document(document))
```

LLM 输出统一由现有结构化输出链路解析为 `PatchSet`。XML 和 Markdown Adapter 不定义路线专属 Patch 协议，`model_output_to_patch` 与 JSON 路线使用同一套目标 ID、字符范围和锚点校验。

`replace` 只输出基于 `WriterBlock.content` 的 `text_range` 和局部 `new_text`。`replace` 和 `delete` 的 `old_text` 在统一校验层从原始 `WriterDocument` 快照填充，模型若提供 `old_text` 则直接拒绝。这可以防止 `<h2>` 或 `##` 等投影标记进入面向核心 DocIR 的 Patch。

三条路线的 Patch prompt 使用同一份 `PatchSet` schema 和同一组输出约束，路线之间只替换文档投影和对应的阅读说明。

---

## 五、实现范围

首版实现只覆盖当前 `WriterDocument` 读取链路已稳定产生的文本类 Block：

- `paragraph`；
- `heading`；
- `list_item`；
- `code`；
- `quote`；
- 上述 Block 中的文本和基本 `WriterSpan.style`。

`table / table_cell` 的类型和目标语法在本方案中保留，但实现应等待表格读取能稳定产生完整单元格内容后再纳入。图片、文件、画板等非文本资源块不在首版实现范围内。

---

## 六、验证与验收

### 6.1 投影转换验证

投影转换只使用一份内容完整的文档做端到端验证。该文档需要同时包含段落、多级标题、嵌套列表、引用、代码块，以及加粗、斜体、下划线、删除线和行内代码等混合行内样式。

端到端验证分别覆盖：

```text
WriterDocument → XML projection → str → parse → WriterDocument
WriterDocument → Markdown projection → str → parse → WriterDocument
```

XML 路线比较往返前后的可编辑语义；Markdown 路线先将不支持的样式静默归一为默认样式，再比较往返结果。

不为单个标签、单个样式或简单确定性转换分别编写冗余测试。

### 6.2 最终验收标准

最终验收使用 `test.py` 文件开头已给定的模型配置、文档配置和真实凭证，不在测试中重复定义另一套配置。

验收时必须设置：

```python
WRITE_BACK = False
```

在不写回飞书的情况下，XML 和 Markdown 两条路线都需要使用真实 LLM 跑通完整流程：

```text
读取真实文档
    → 转换为 XML / Markdown 投影
    → 生成 ModifyPlan
    → 生成统一 PatchSet
    → apply_patch 应用到内存中的 WriterDocument
    → 校验 PatchResult 和修改后文档
```

`WRITE_BACK=False` 时不得调用飞书写回逻辑。LLM 调用不编写 Mock，验收结果以真实模型能否完成两条端到端链路为准。
