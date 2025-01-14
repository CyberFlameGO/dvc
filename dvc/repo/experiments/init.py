import logging
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Dict, Iterable, cast

from funcy import compact, lremove, post_processing
from rich.prompt import Prompt
from rich.rule import Rule
from rich.syntax import Syntax

from dvc.exceptions import DvcException
from dvc.stage import PipelineStage
from dvc.stage.serialize import to_pipeline_file
from dvc.types import OptStr
from dvc.utils.serialize import dumps_yaml

if TYPE_CHECKING:
    from dvc.repo import Repo
    from dvc.dvcfile import DVCFile
    from dvc.stage import Stage

from dvc.ui import ui

PROMPTS = {
    "cmd": "[b]Command[/b] to execute",
    "code": "Path to a [b]code[/b] file/directory",
    "data": "Path to a [b]data[/b] file/directory",
    "models": "Path to a [b]model[/b] file/directory",
    "params": "Path to a [b]parameters[/b] file",
    "metrics": "Path to a [b]metrics[/b] file",
    "plots": "Path to a [b]plots[/b] file/directory",
    "live": "Path to log [b]dvclive[/b] outputs",
}


class RequiredPrompt(Prompt):
    def process_response(self, value: str):
        from rich.prompt import InvalidResponse

        ret = super().process_response(value)
        if not ret:
            raise InvalidResponse(
                "[prompt.invalid]Response required. Please try again."
            )
        return ret

    @classmethod
    def get_input(cls, *args, **kwargs) -> str:
        try:
            return super().get_input(*args, **kwargs)
        except (KeyboardInterrupt, EOFError):
            ui.error_write()
            raise

    def render_default(self, default):
        from rich.text import Text

        return Text(f"{default!s}", "green")


class SkippablePrompt(RequiredPrompt):
    skip_value: str = "n"

    def process_response(self, value: str):
        ret = super().process_response(value)
        return None if ret == self.skip_value else ret

    def make_prompt(self, default):
        prompt = self.prompt.copy()
        prompt.end = ""

        prompt.append(" [")
        if (
            default is not ...
            and self.show_default
            and isinstance(default, (str, self.response_type))
        ):
            _default = self.render_default(default)
            prompt.append(_default)
            prompt.append(", ")

        prompt.append(f"{self.skip_value} to omit", style="italic")
        prompt.append("]")
        prompt.append(self.prompt_suffix)
        return prompt


@post_processing(dict)
def _prompts(keys: Iterable[str], defaults: Dict[str, OptStr]):
    for key in keys:
        if key == "cmd":
            prompt_cls = RequiredPrompt
        else:
            prompt_cls = SkippablePrompt
        kwargs = {"default": defaults[key]} if key in defaults else {}
        prompt = PROMPTS[key]
        value = prompt_cls.ask(  # type: ignore[call-overload]
            prompt, console=ui.error_console, **kwargs
        )
        yield key, value


@contextmanager
def _disable_logging(highest_level=logging.CRITICAL):
    previous_level = logging.root.manager.disable

    logging.disable(highest_level)

    try:
        yield
    finally:
        logging.disable(previous_level)


PIPELINE_FILE_LINK = (
    "https://dvc.org/doc/user-guide/project-structure/pipelines-files"
)


def init_interactive(
    name: str,
    defaults: Dict[str, str],
    provided: Dict[str, str],
    show_tree: bool = False,
    live: bool = False,
) -> Dict[str, str]:
    primary = lremove(
        provided.keys(), ["cmd", "code", "data", "models", "params"]
    )
    secondary = lremove(
        provided.keys(), ["live"] if live else ["metrics", "plots"]
    )

    if not (primary or secondary):
        return {}

    message = ui.rich_text.assemble(
        "This command will guide you to set up a ",
        (name, "bright_blue"),
        " stage in ",
        ("dvc.yaml", "green"),
        ".",
    )
    doc_link = ui.rich_text.assemble(
        "See ", (PIPELINE_FILE_LINK, "repr.url"), "."
    )
    ui.error_write(message, doc_link, "", sep="\n", styled=True)

    if show_tree:
        from rich.tree import Tree

        tree = Tree(
            "DVC assumes the following workspace structure:",
            highlight=True,
        )
        workspace = {**defaults, **provided}
        workspace.pop("cmd", None)
        if not live and "live" not in provided:
            workspace.pop("live", None)
        for value in sorted(workspace.values()):
            tree.add(f"[green]{value}[/green]")
        ui.error_write(tree, styled=True)
        ui.error_write()

    return compact(
        {
            **_prompts(primary, defaults),
            **_prompts(secondary, defaults),
        }
    )


def _check_stage_exists(
    dvcfile: "DVCFile", name: str, force: bool = False
) -> None:
    if not force and dvcfile.exists() and name in dvcfile.stages:
        from dvc.stage.exceptions import DuplicateStageName

        hint = "Use '--force' to overwrite."
        raise DuplicateStageName(
            f"Stage '{name}' already exists in 'dvc.yaml'. {hint}"
        )


def init(
    repo: "Repo",
    name: str = None,
    type: str = "default",  # pylint: disable=redefined-builtin
    defaults: Dict[str, str] = None,
    overrides: Dict[str, str] = None,
    interactive: bool = False,
    force: bool = False,
) -> "Stage":
    from dvc.dvcfile import make_dvcfile

    dvcfile = make_dvcfile(repo, "dvc.yaml")
    name = name or type

    _check_stage_exists(dvcfile, name, force=force)

    defaults = defaults or {}
    overrides = overrides or {}

    with_live = type == "live"
    if interactive:
        defaults = init_interactive(
            name,
            defaults=defaults,
            live=with_live,
            provided=overrides,
            show_tree=True,
        )
    else:
        if with_live:
            # suppress `metrics`/`params` if live is selected, unless
            # it is also provided via overrides/cli.
            # This makes output to be a checkpoint as well.
            defaults.pop("metrics")
            defaults.pop("params")
        else:
            defaults.pop("live")  # suppress live otherwise

    context: Dict[str, str] = {**defaults, **overrides}
    assert "cmd" in context

    params_kv = []
    if context.get("params"):
        from dvc.utils.serialize import LOADERS

        path = context["params"]
        assert isinstance(path, str)
        _, ext = os.path.splitext(path)
        params_kv = [{path: list(LOADERS[ext](path))}]

    checkpoint_out = bool(context.get("live"))
    models = context.get("models")
    stage = repo.stage.create(
        name=name,
        cmd=context["cmd"],
        deps=compact([context.get("code"), context.get("data")]),
        params=params_kv,
        metrics_no_cache=compact([context.get("metrics")]),
        plots_no_cache=compact([context.get("plots")]),
        live=context.get("live"),
        force=force,
        **{"checkpoints" if checkpoint_out else "outs": compact([models])},
    )

    if interactive:
        ui.write(Rule(style="green"), styled=True)
        _yaml = dumps_yaml(to_pipeline_file(cast(PipelineStage, stage)))
        syn = Syntax(_yaml, "yaml", theme="ansi_dark")
        ui.error_write(syn, styled=True)

    if not interactive or ui.confirm(
        "Do you want to add the above contents to dvc.yaml?"
    ):
        with _disable_logging():
            stage.dump(update_lock=False)
        stage.ignore_outs()
    else:
        raise DvcException("Aborting ...")
    return stage
