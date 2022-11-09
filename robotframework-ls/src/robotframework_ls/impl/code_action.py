from typing import List, Dict, Any, Iterator, Optional
import typing

from robocorp_ls_core.lsp import (
    CommandTypedDict,
    ICustomDiagnosticDataTypedDict,
    ICustomDiagnosticDataUndefinedKeywordTypedDict,
    WorkspaceEditTypedDict,
    CompletionItemTypedDict,
    TextEditTypedDict,
    WorkspaceEditParamsTypedDict,
    ICustomDiagnosticDataUndefinedResourceTypedDict,
    RangeTypedDict,
    ShowDocumentParamsTypedDict,
    ICustomDiagnosticDataUndefinedLibraryTypedDict,
    ICustomDiagnosticDataUndefinedVarImportTypedDict,
)
from robotframework_ls.impl.protocols import (
    ICompletionContext,
    IKeywordFound,
    IResourceImportNode,
)
from robocorp_ls_core.robotframework_log import get_logger
from robocorp_ls_core.basic import isinstance_name
import os
from pathlib import Path

log = get_logger(__name__)


def _add_import_code_action(
    completion_context: ICompletionContext,
) -> Iterator[CommandTypedDict]:
    from robotframework_ls.impl.collect_keywords import (
        collect_keyword_name_to_keyword_found,
    )
    from robotframework_ls.impl import auto_import_completions

    keyword_name_to_keyword_found: Dict[
        str, List[IKeywordFound]
    ] = collect_keyword_name_to_keyword_found(completion_context)
    auto_imports_found: List[
        CompletionItemTypedDict
    ] = auto_import_completions.complete(
        completion_context, keyword_name_to_keyword_found, exact_match=True
    )

    for auto_import in auto_imports_found:
        label = auto_import["label"]
        if label.endswith("*"):
            label = label[:-1]

        lst: List[TextEditTypedDict] = []

        text_edit = auto_import["textEdit"]
        if text_edit:
            lst.append(text_edit)

        additional = auto_import["additionalTextEdits"]
        if additional:
            lst.extend(additional)

        changes = {completion_context.doc.uri: lst}
        edit: WorkspaceEditTypedDict = {"changes": changes}
        title = f"Import {label}"
        edit_params: WorkspaceEditParamsTypedDict = {"edit": edit, "label": title}
        command: CommandTypedDict = {
            "title": title,
            "command": "robot.applyCodeAction",
            "arguments": [{"apply_edit": edit_params}],
        }

        yield command


def _create_keyword_in_current_file_text_edit(
    completion_context: ICompletionContext,
    keyword_template: str,
) -> TextEditTypedDict:
    from robotframework_ls.impl import ast_utils

    current_section: Any = completion_context.get_ast_current_section()
    if ast_utils.is_keyword_section(current_section):
        # Add it before the current keyword
        use_line = None
        for node in current_section.body:
            if isinstance_name(node, "Keyword"):
                node_lineno = node.lineno - 1

                if node_lineno <= completion_context.sel.line:
                    use_line = node_lineno
                else:
                    break

        if use_line is not None:
            return {
                "range": {
                    "start": {"line": use_line, "character": 0},
                    "end": {"line": use_line, "character": 0},
                },
                "newText": keyword_template,
            }

    keyword_section = ast_utils.find_keyword_section(completion_context.get_ast())
    if keyword_section is None:
        # We need to create the keyword section too
        current_section = completion_context.get_ast_current_section()
        if current_section is None:
            use_line = 0
        else:
            use_line = current_section.lineno - 1

        return {
            "range": {
                "start": {"line": use_line, "character": 0},
                "end": {"line": use_line, "character": 0},
            },
            "newText": f"*** Keywords ***\n{keyword_template}",
        }

    else:
        # We add the keyword to the end of the existing keyword section
        use_line = keyword_section.end_lineno
        return {
            "range": {
                "start": {"line": use_line, "character": 0},
                "end": {"line": use_line, "character": 0},
            },
            "newText": keyword_template,
        }


def _create_keyword_in_current_file_code_action(
    completion_context: ICompletionContext, keyword_template: str, keyword_name: str
) -> Iterator[CommandTypedDict]:

    text_edit = _create_keyword_in_current_file_text_edit(
        completion_context, keyword_template
    )
    lst: List[TextEditTypedDict] = [text_edit]

    changes = {completion_context.doc.uri: lst}
    edit: WorkspaceEditTypedDict = {"changes": changes}
    title = f"Create Keyword: {keyword_name} (in current file)"
    edit_params: WorkspaceEditParamsTypedDict = {"edit": edit, "label": title}
    command: CommandTypedDict = {
        "title": title,
        "command": "robot.applyCodeAction",
        "arguments": [{"apply_edit": edit_params}],
    }

    _add_show_document_at_command(command, completion_context.doc.uri, text_edit)
    yield command


def _undefined_resource_code_action(
    completion_context: ICompletionContext,
    undefined_resource_data: ICustomDiagnosticDataUndefinedResourceTypedDict,
) -> Iterator[CommandTypedDict]:
    from robocorp_ls_core.lsp import CreateFileTypedDict
    from robocorp_ls_core import uris

    name = undefined_resource_data["resolved_name"]
    if not name:
        name = undefined_resource_data["name"]
        if not name:
            return

    if "$" in name or "{" in name or "}" in name:
        return

    path = Path(os.path.join(os.path.dirname(completion_context.doc.path), name))
    doc_uri = uris.from_fs_path(str(path))
    create_doc_change: CreateFileTypedDict = {
        "kind": "create",
        "uri": doc_uri,
    }
    edit: WorkspaceEditTypedDict = {"documentChanges": [create_doc_change]}
    title: str = f"Create {path.name} (at {path.parent})"
    edit_params: WorkspaceEditParamsTypedDict = {"edit": edit, "label": title}

    command: CommandTypedDict = {
        "title": title,
        "command": "robot.applyCodeAction",
        "arguments": [{"apply_edit": edit_params}],
    }

    _add_show_document_at_command(command, doc_uri)

    yield command


def _undefined_keyword_code_action(
    completion_context: ICompletionContext,
    undefined_keyword_data: ICustomDiagnosticDataUndefinedKeywordTypedDict,
) -> Iterator[CommandTypedDict]:
    from robotframework_ls.robot_config import get_arguments_separator

    keyword_template = """$keyword_name$arguments\n\n"""

    # --- Update the arguments in the template.

    arguments: List[str] = []
    keyword_usage_info = completion_context.get_current_keyword_usage_info()
    if keyword_usage_info is not None:
        for token in keyword_usage_info.node.tokens:
            if token.type == token.ARGUMENT:
                i = token.value.find("=")
                if i > 0:
                    name = token.value[:i]
                else:
                    name = token.value
                if not name:
                    name = "arg"
                arguments.append(f"${{{name}}}")

    separator = get_arguments_separator(completion_context)
    args_str = ""
    if arguments:
        args_str += "\n    [Arguments]"
        for arg in arguments:
            args_str += separator
            args_str += arg
        args_str += "\n"

    keyword_template = keyword_template.replace("$arguments", args_str)

    # --- Update the keyword name in the template.

    # We'd like to have a cursor here, but alas, this isn't possible...
    # See: https://github.com/microsoft/language-server-protocol/issues/592
    # See: https://github.com/microsoft/language-server-protocol/issues/724
    keyword_name = undefined_keyword_data["name"]

    dots_found = keyword_name.count(".")
    if dots_found >= 2:
        # Must check for use cases... Do nothing for now.
        return

    if dots_found == 1:
        # Something as:
        # my_resource.Keyword or
        # my_python_module.Keyword
        #
        # in this case we need to create a keyword "Keyword" in "my_resource".
        # If my_module is imported, create it in that module, otherwise,
        # if it exists but we haven't imported it, we need to import it.
        # If it doesn't exist we need to create it first.
        splitted = keyword_name.split(".")
        resource_or_import_or_alias_name, keyword_name = splitted
        keyword_template = keyword_template.replace("$keyword_name", keyword_name)
        yield from _deal_with_resource_or_import_or_alias_name(
            completion_context,
            resource_or_import_or_alias_name,
            keyword_template,
            keyword_name,
        )
        return

    keyword_template = keyword_template.replace("$keyword_name", keyword_name)

    yield from _add_import_code_action(completion_context)
    yield from _create_keyword_in_current_file_code_action(
        completion_context, keyword_template, keyword_name
    )


def _matches_resource_import(
    resource_import: IResourceImportNode,
    name: str,
):
    from robotframework_ls.impl.text_utilities import normalize_robot_name

    name = normalize_robot_name(name)

    for token in resource_import.tokens:
        if token.type == token.NAME:
            import_name = normalize_robot_name(token.value)

            if import_name == name:
                return True

            # ./my_resource.robot -> my_resource.robot
            import_name = os.path.basename(import_name)
            if import_name == name:
                return True

            # Handle something as my_resource.robot
            import_name = os.path.splitext(import_name)[0]
            if import_name == name:
                return True

    return False


def _create_keyword_in_another_file_code_action(
    completion_context: ICompletionContext, keyword_template: str, keyword_name: str
) -> Iterator[CommandTypedDict]:

    text_edit = _create_keyword_in_current_file_text_edit(
        completion_context, keyword_template
    )
    lst: List[TextEditTypedDict] = [text_edit]

    changes = {completion_context.doc.uri: lst}
    edit: WorkspaceEditTypedDict = {"changes": changes}
    modname: str = os.path.basename(completion_context.doc.uri)
    title = f"Create Keyword: {keyword_name} (in {modname})"
    edit_params: WorkspaceEditParamsTypedDict = {"edit": edit, "label": title}

    command: CommandTypedDict = {
        "title": title,
        "command": "robot.applyCodeAction",
        "arguments": [
            {
                "apply_edit": edit_params,
            }
        ],
    }

    _add_show_document_at_command(command, completion_context.doc.uri, text_edit)

    yield command


def _add_show_document_at_command(
    command: CommandTypedDict,
    doc_uri: str,
    text_edit: Optional[TextEditTypedDict] = None,
):
    if text_edit:
        endline = text_edit["range"]["end"]["line"]
        endchar = text_edit["range"]["end"]["character"]
    else:
        endline = 0
        endchar = 0

    selection: RangeTypedDict = {
        "start": {"line": endline, "character": endchar},
        "end": {"line": endline, "character": endchar},
    }
    show_document: ShowDocumentParamsTypedDict = {
        "uri": doc_uri,
        "selection": selection,
        "takeFocus": True,
    }

    arguments = command["arguments"]
    if arguments:
        arguments[0]["show_document"] = show_document


def _deal_with_resource_or_import_or_alias_name(
    completion_context: ICompletionContext,
    resource_or_import_or_alias_name: str,
    keyword_template: str,
    keyword_name: str,
):
    for resource_import in completion_context.get_resource_imports():
        if _matches_resource_import(resource_import, resource_or_import_or_alias_name):
            doc = completion_context.get_resource_import_as_doc(resource_import)
            if doc is not None:
                new_completion_context = completion_context.create_copy(doc)
                yield from _create_keyword_in_another_file_code_action(
                    new_completion_context, keyword_template, keyword_name
                )


def code_action(
    completion_context: ICompletionContext,
    found_data: List[ICustomDiagnosticDataTypedDict],
) -> List[CommandTypedDict]:
    """
    Note: the completion context selection should be at the range end position.
    """

    ret: List[CommandTypedDict] = []
    for data in found_data:
        if data["kind"] == "undefined_keyword":
            undefined_keyword_data = typing.cast(
                ICustomDiagnosticDataUndefinedKeywordTypedDict, data
            )
            ret.extend(
                _undefined_keyword_code_action(
                    completion_context, undefined_keyword_data
                )
            )
        elif data["kind"] == "undefined_resource":
            undefined_resource_data = typing.cast(
                ICustomDiagnosticDataUndefinedResourceTypedDict, data
            )
            ret.extend(
                _undefined_resource_code_action(
                    completion_context, undefined_resource_data
                )
            )

        elif data["kind"] == "undefined_library":
            undefined_library_data = typing.cast(
                ICustomDiagnosticDataUndefinedLibraryTypedDict, data
            )
            ret.extend(
                _undefined_resource_code_action(
                    completion_context, undefined_library_data
                )
            )

        elif data["kind"] == "undefined_var_import":
            undefined_var_import_data = typing.cast(
                ICustomDiagnosticDataUndefinedVarImportTypedDict, data
            )
            ret.extend(
                _undefined_resource_code_action(
                    completion_context, undefined_var_import_data
                )
            )

    for r in ret:
        if r["command"] == "robot.applyCodeAction":
            arguments = r["arguments"]
            if arguments:
                arg = arguments[0]
                lint_uris = arg.get("lint_uris")
                if lint_uris is None:
                    lint_uris = []
                    arg["lint_uris"] = lint_uris
                lint_uris.append(completion_context.doc.uri)

    return ret