"""Shared, non-executable descriptions of missing or unverified inputs."""
from __future__ import annotations


def field(path, question, expected, *, source='user', code='missing_input', selector=None):
    result = dict(path=path, question=question, expected_format=expected, source=source, code=code)
    if selector:
        result['selector'] = selector
    return result


def guidance(tool, fields, preserve=(), arguments=None):
    return dict(version=1, fields=fields, ask_together=True, mutation_authorized=False,
                resume=dict(tool=tool, arguments=arguments or {}, arguments_complete=False,
                            preserve_arguments=list(preserve), requires_complete_arguments=True,
                            ready_to_call=False),
                note='按字段来源补齐或核实，保留原参数后重新调用完整请求；本提示不保存输入、不自动填写或执行。')


def attach(result, tool, fields, preserve=(), arguments=None):
    return {**result, 'input_guidance': guidance(tool, fields, preserve, arguments)}
