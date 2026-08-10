import json
import time
from copy import copy
from pathlib import Path

from lazyllm.module.module import ModuleBase
from lazyllm.tools.writer.data_models import (
    TargetDocument,
    WriterBlock,
    WriterDocument,
    WritingContext,
    WritingTask,
)
from lazyllm.tools.writer.tools.outline_stream_tools import WriterOutlineStreamingTools
from lazyllm.tools.writer.utils import load_artifact_json, render_document_markdown


class _StreamingLLM(ModuleBase):
    def __init__(self, chunks, *, response=None):
        super().__init__()
        self._chunks = chunks
        self._response = response
        self._stream = False

    def share(self, stream=None):
        shared = copy(self)
        if stream is not None:
            shared._stream = stream
        return shared

    def forward(self, prompt):
        with self.stream_output(self._stream):
            for tag, delta in self._chunks:
                self._stream_output(delta, cls=tag)
                time.sleep(0.001)
        if self._response is not None:
            return self._response
        return "".join(delta for tag, delta in self._chunks if tag == "text")


def test_stream_markdown_outline_returns_the_authoritative_artifact(tmp_path):
    chunks = [
        ("think", "provider reasoning"),
        ("text", "<think>hidden</think>\n\n# 测试大纲\n\n## 第一章"),
        ("text", "\n\n- 要点一\n\n## 第二章\n\n- 要点二"),
    ]
    task = WritingTask(
        task_id="outline-markdown",
        query="生成测试大纲",
        task_type="write",
        output={"representation": "markdown"},
    )
    tool = WriterOutlineStreamingTools(
        llm=_StreamingLLM(chunks),
        artifact_store=str(tmp_path),
    )

    with tool.stream_outline(
        task, WritingContext(context_id="ctx-md"), idle_timeout=1
    ) as stream:
        preview = "".join(stream)
        result = stream.result()

    assert preview == "# 测试大纲\n\n## 第一章\n\n- 要点一\n\n## 第二章\n\n- 要点二\n"
    assert Path(result["artifact_path"]).read_text(encoding="utf-8") == preview
    assert result["metadata"]["extra"]["representation"] == "markdown"


def test_stream_ir_outline_exposes_markdown_and_saves_validated_document(tmp_path):
    response_document = WriterDocument(
        document_id="model-document-id",
        stage="outline",
        title="模型标题",
        blocks=[
            WriterBlock(
                node_id=f"section-{index}",
                type="heading",
                content=f"第{index}章",
                stage="outline",
                children=[
                    WriterBlock(
                        node_id=f"point-{index}",
                        type="list_item",
                        content=f"要点{index}",
                        stage="outline",
                    )
                ],
            )
            for index in range(1, 4)
        ],
    )
    response = json.dumps(
        response_document.model_dump(exclude_defaults=True),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    split_at = response.index("要点2") + 2
    task = WritingTask(
        task_id="outline-ir",
        query="生成结构化大纲",
        task_type="write",
        output={"representation": "ir"},
        target_document=TargetDocument(title="权威标题"),
    )
    tool = WriterOutlineStreamingTools(
        llm=_StreamingLLM(
            [
                ("text", response[:split_at]),
                ("text", response[split_at:]),
            ],
            response=response,
        ),
        artifact_store=str(tmp_path),
    )

    with tool.stream_outline(
        task, WritingContext(context_id="ctx-ir"), idle_timeout=1
    ) as stream:
        deltas = list(stream)
        result = stream.result()

    document = load_artifact_json(result["artifact_path"], WriterDocument)
    assert document.document_id == "ctx-ir-outline"
    assert document.title == "权威标题"
    assert document.stage == "outline"
    assert document.ui_editable is False
    assert "".join(deltas) == render_document_markdown(document)
    assert deltas[0] == "# 权威标题"
    assert any("要点1" in delta for delta in deltas[:-1])
