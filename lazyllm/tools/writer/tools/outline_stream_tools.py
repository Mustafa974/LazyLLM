from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from ..data_models.context import WritingContext
from ..data_models.resource import ResourceProfile
from ..data_models.task import WritingTask
from ..data_models.writer_ir import WriterDocument
from ..prompts import GENERATE_OUTLINE_MARKDOWN_PROMPT, GENERATE_OUTLINE_PROMPT
from ..utils import (
    get_markdown_outline_targets,
    make_markdown_tool_result,
    parse_markdown_sections,
    render_document_markdown,
    to_prompt_json,
)
from .planning_tools import WriterPlanningTools
from .stream_tools import (
    DraftPreviewStream,
    IRBlockStreamState,
    IRJSONMarkdownParser,
    IRPreviewOutput,
    MarkdownStreamNormalizer,
)


class OutlineMarkdownStream(DraftPreviewStream):
    def __init__(
        self,
        call: Callable[[Callable[[dict[str, Any]], None]], str],
        finalize: Callable[[str], dict],
        idle_timeout: float,
    ):
        normalizer = MarkdownStreamNormalizer()

        def consume(payload: dict[str, Any]) -> list[str]:
            if payload.get("tag") != "text":
                return []
            return normalizer.feed(str(payload.get("delta") or ""))

        def finish(response: Any) -> tuple[list[str], dict]:
            body = str(response).strip()
            deltas = normalizer.finish()
            if normalizer.body != body:
                raise ValueError(
                    "Streamed Markdown outline does not match the normalized LLM response."
                )
            return [*deltas, "\n"], finalize(body)

        super().__init__(
            call=call,
            consume=consume,
            finalize=finish,
            idle_timeout=idle_timeout,
            label="Outline Markdown",
        )


class _OutlineIRPreviewOutput(IRPreviewOutput):
    @property
    def has_body(self) -> bool:
        return self._has_body

    def mark_body(self) -> None:
        self._has_body = True


class OutlineIRJSONMarkdownParser(IRJSONMarkdownParser):
    """Expose a streamed WriterDocument as a safe Markdown outline preview."""

    def __init__(self, preview_title: str | None = None):
        self.prefix = ""
        self._delta_parts: list[str] = []
        self._emitted_parts: list[str] = []
        self._preview_ready = False
        self._pending_parts: list[str] = []
        self.output = _OutlineIRPreviewOutput("", self.emit)
        self.raw_json = ""
        self._started = False
        self._done = False
        self._stack: list[dict[str, Any]] = []
        self._in_string = False
        self._string_kind = ""
        self._string_context: dict[str, Any] | None = None
        self._string_key: str | None = None
        self._string_parts: list[str] = []
        self._escape = False
        self._unicode_digits: str | None = None
        self._pending_high_surrogate: int | None = None
        self._primitive = False
        if preview_title:
            self._activate_preview(preview_title)
        self.initial_deltas = list(self._delta_parts)
        self._delta_parts = []

    def emit(self, text: str) -> None:
        if not text:
            return
        if not self._preview_ready:
            self._pending_parts.append(text)
            return
        super().emit(text)

    def finish_document(self, document: WriterDocument) -> list[str]:
        self._delta_parts = []
        if not self._preview_ready:
            self._activate_preview(document.title)
        final_markdown = render_document_markdown(document)
        emitted = "".join(self._emitted_parts)
        if final_markdown.startswith(emitted):
            self.emit(final_markdown[len(emitted) :])
        elif final_markdown.rstrip() != emitted.rstrip():
            raise ValueError(
                "Streamed IR outline does not match the validated WriterDocument."
            )
        return ["".join(self._delta_parts)] if self._delta_parts else []

    def _activate_preview(self, title: str) -> None:
        if self._preview_ready:
            return
        pending = "".join(self._pending_parts)
        self._pending_parts = []
        self._preview_ready = True
        clean_title = title.strip()
        if clean_title:
            super().emit(f"# {clean_title}")
            if self.output.has_body and pending:
                super().emit("\n\n")
            elif not self.output.has_body:
                self.output.mark_body()
        if pending:
            super().emit(pending)

    def _feed_char(self, char: str) -> None:
        if not self._started:
            if char != "{":
                return
            self._started = True
            self.raw_json = "{"
            root = IRBlockStreamState(self, start_index=0, level=1, root=True)
            self._stack.append(
                {
                    "kind": "object",
                    "expect": "key_or_end",
                    "key": None,
                    "block": root,
                }
            )
            return
        super()._feed_char(char)

    def _emit_string_text(self, text: str) -> None:
        context = self._string_context
        block = context.get("block") if context else None
        if (
            self._string_kind == "key"
            or self._string_key == "type"
            or (self._string_key == "title" and block is not None and block.root)
        ):
            self._string_parts.append(text)
            return
        if self._string_key == "content" and block is not None:
            block.feed_content(text)

    def _finish_string(self) -> None:
        context = self._string_context
        block = context.get("block") if context else None
        is_title = (
            self._string_kind == "value"
            and self._string_key == "title"
            and block is not None
            and block.root
        )
        title = "".join(self._string_parts) if is_title else ""
        super()._finish_string()
        if is_title:
            self._activate_preview(title)

    def _start_object(self) -> None:
        parent = self._stack[-1] if self._stack else None
        role, owner, suppressed = self._value_context(parent)
        self._mark_value_started()
        block = None
        if (
            parent
            and parent["kind"] == "array"
            and owner is not None
            and (role == "children" or (owner.root and role == "blocks"))
        ):
            block = IRBlockStreamState(
                self,
                start_index=len(self.raw_json) - 1,
                level=owner.level + 1,
                suppressed=suppressed,
            )
        self._stack.append(
            {
                "kind": "object",
                "expect": "key_or_end",
                "key": None,
                "block": block,
                "metadata_role": role
                if role == "numbering" and owner is not None
                else None,
                "metadata_owner": owner,
                "start_index": len(self.raw_json) - 1,
            }
        )

    def _start_array(self) -> None:
        parent = self._stack[-1] if self._stack else None
        role, owner, suppressed = self._value_context(parent)
        self._mark_value_started()
        if owner is not None and (
            role == "children" or (owner.root and role == "blocks")
        ):
            suppressed = suppressed or owner.prepare_children()
        self._stack.append(
            {
                "kind": "array",
                "expect": "value_or_end",
                "role": role,
                "owner": owner,
                "suppressed": suppressed,
            }
        )


class OutlineIRStream(DraftPreviewStream):
    def __init__(
        self,
        call: Callable[[Callable[[dict[str, Any]], None]], WriterDocument],
        normalize: Callable[[WriterDocument], WriterDocument],
        finalize: Callable[[WriterDocument], dict],
        idle_timeout: float,
        preview_title: str | None = None,
    ):
        parser = OutlineIRJSONMarkdownParser(preview_title)

        def consume(payload: dict[str, Any]) -> list[str]:
            if payload.get("tag") != "text":
                return []
            return parser.feed(str(payload.get("delta") or ""))

        def finish(response: Any) -> tuple[list[str], dict]:
            if not isinstance(response, WriterDocument):
                response = WriterDocument.model_validate(response)
            document = normalize(response)
            return parser.finish_document(document), finalize(document)

        super().__init__(
            call=call,
            consume=consume,
            finalize=finish,
            idle_timeout=idle_timeout,
            initial_deltas=parser.initial_deltas,
            label="Outline IR",
        )


class WriterOutlineStreamingTools(WriterPlanningTools):
    """Non-tool streaming companion for WriterPlanningTools.generate_outline."""

    def stream_outline(
        self,
        task: Any,
        context: Any,
        resource_profiles: Any = None,
        execution_results: Any = None,
        representation: Literal["ir", "markdown"] | None = None,
        *,
        idle_timeout: float | None = None,
    ) -> DraftPreviewStream:
        writing_task = self._unified_model(task, WritingTask)
        writing_context = self._unified_model(context, WritingContext)
        profiles = self._unified_models(resource_profiles, ResourceProfile)
        execution_data = self._unified_raw_data(execution_results)
        resolved_representation = self._resolve_representation(
            writing_task, representation
        )
        timeout = self._outline_stream_idle_timeout(idle_timeout)

        if resolved_representation == "markdown":
            prompt = GENERATE_OUTLINE_MARKDOWN_PROMPT.format(
                task_json=to_prompt_json(writing_task),
                context_json=to_prompt_json(writing_context),
                resource_profiles_json=to_prompt_json(profiles),
                execution_results_json=to_prompt_json(execution_data),
            )
            return OutlineMarkdownStream(
                call=lambda sink: self._call_llm_text(
                    prompt,
                    stream_output={"_stream_sink": sink},
                ),
                finalize=lambda outline: self._save_markdown_outline(
                    outline,
                    writing_task,
                    writing_context,
                    profiles,
                    execution_data,
                ),
                idle_timeout=timeout,
            )

        document_id = f"{writing_context.context_id}-outline"
        prompt = GENERATE_OUTLINE_PROMPT.format(
            task_json=to_prompt_json(writing_task),
            document_id=document_id,
            context_json=to_prompt_json(writing_context),
            resource_profiles_json=to_prompt_json(profiles),
            execution_results_json=to_prompt_json(execution_data),
        )
        preview_title = (
            writing_task.target_document.title
            if writing_task.target_document and writing_task.target_document.title
            else None
        )
        return OutlineIRStream(
            call=lambda sink: self._call_llm_structured(
                prompt,
                WriterDocument,
                stream_output={"_stream_sink": sink},
            ),
            normalize=lambda outline: self._normalize_streamed_outline(
                outline,
                document_id,
                writing_task,
                writing_context,
                profiles,
            ),
            finalize=lambda outline: self._save_ir_outline(
                outline,
                writing_task,
                writing_context,
                profiles,
                execution_data,
            ),
            idle_timeout=timeout,
            preview_title=preview_title,
        )

    def _save_markdown_outline(
        self,
        outline: str,
        task: WritingTask,
        context: WritingContext,
        profiles: list[ResourceProfile],
        execution_data: Any,
    ) -> dict:
        outline = outline.strip() + "\n"
        _, targets = get_markdown_outline_targets(outline)
        path = self._write_markdown_artifact("outline.md", outline)
        return make_markdown_tool_result(
            path=path,
            step_name="generate_outline",
            artifact_key="outline",
            summary="Generated writing outline as Markdown.",
            counts={
                "top_level_sections": len(targets),
                "outline_nodes": len(parse_markdown_sections(outline)),
                "characters": len(outline),
            },
            extra={
                "representation": "markdown",
                "task_id": task.task_id,
                "context_id": context.context_id,
                "resource_profile_count": len(profiles),
                "has_execution_results": execution_data is not None,
            },
        ).model_dump()

    def _normalize_streamed_outline(
        self,
        outline: WriterDocument,
        document_id: str,
        task: WritingTask,
        context: WritingContext,
        profiles: list[ResourceProfile],
    ) -> WriterDocument:
        outline.document_id = document_id
        return self._normalize_outline(outline, task, context, profiles)

    def _save_ir_outline(
        self,
        outline: WriterDocument,
        task: WritingTask,
        context: WritingContext,
        profiles: list[ResourceProfile],
        execution_data: Any,
    ) -> dict:
        return self._save_artifacts(
            {"outline": outline},
            step_name="generate_outline",
            primary_key="outline",
            context_key=None,
            summary="Generated writing outline.",
            counts={
                "top_level_sections": len(outline.blocks),
                "outline_nodes": len(list(outline.iter_blocks())),
            },
            extra={"representation": "ir"},
            artifact_meta={
                "task_id": task.task_id,
                "context_id": context.context_id,
                "resource_profile_count": len(profiles),
                "has_execution_results": execution_data is not None,
            },
        ).model_dump()

    def _outline_stream_idle_timeout(self, idle_timeout: float | None) -> float:
        value: Any = idle_timeout
        if value is None:
            value = getattr(self.llm, "_timeout", None)
        if isinstance(value, (tuple, list)):
            value = value[-1] if value else None
        if value is None:
            value = 180.0
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError("idle_timeout must be a positive number.")
        return float(value)


__all__ = [
    "OutlineIRJSONMarkdownParser",
    "OutlineIRStream",
    "OutlineMarkdownStream",
    "WriterOutlineStreamingTools",
]
