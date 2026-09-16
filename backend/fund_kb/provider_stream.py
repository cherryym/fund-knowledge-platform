"""Bounded Responses SSE completion collector; no network or tool execution.

Only the full terminal response is returned. Deltas and private reasoning are
never logged, displayed, persisted, or used as a completed business result.
"""
import codecs
import json

from .answer_preview import PublicTextBuffer


class StreamError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ResponseStream:
    def __init__(self, max_bytes, *, allow_data_envelope=False, inspect_event=None, _on_public_text=None, secrets=()):
        self.max_bytes = max_bytes
        self.bytes = 0
        self.decoder = codecs.getincrementaldecoder('utf-8')()
        self.buffer = ''
        self.data = []
        self.event = ''
        self.result = None
        self.allow_data_envelope = allow_data_envelope
        self.inspect_event = inspect_event
        self.preview = PublicTextBuffer(_on_public_text, max_bytes=max_bytes, secrets=secrets) \
            if callable(_on_public_text) and not allow_data_envelope else None
        self.public_item = None
        self.public_index = None

    def feed(self, chunk):
        try:
            return self._feed(chunk)
        except BaseException:
            if self.preview:
                self.preview.revoke()
            raise

    def _feed(self, chunk):
        self.bytes += len(chunk)
        if self.bytes > self.max_bytes:
            raise StreamError('PROVIDER_RESPONSE_TOO_LARGE')
        try:
            self.buffer += self.decoder.decode(chunk)
        except UnicodeError:
            raise StreamError('PROVIDER_STREAM_INVALID') from None
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            self._line(line.removesuffix('\r'))
            if self.result is not None:
                # Validate the bytes already consumed, including an incomplete
                # UTF-8 suffix. Future bytes are outside the terminal boundary.
                try:
                    self.decoder.decode(b'', final=True)
                except UnicodeError:
                    raise StreamError('PROVIDER_STREAM_INVALID') from None
                return self.result
        # Inspect every full event already in this chunk before publishing.
        if self.preview and not self.buffer and not self.data:
            self.preview.flush()
        return None

    def _line(self, line):
        if line.startswith('data:'):
            self.data.append(line[5:].lstrip(' '))
        elif line.startswith('event:'):
            self.event = line[6:].strip()
        elif not line:
            data, event = '\n'.join(self.data), self.event
            self.data, self.event = [], ''
            if not data:
                return
            if data == '[DONE]':
                if self.result is None:
                    raise StreamError('PROVIDER_STREAM_INCOMPLETE')
                return
            try:
                parsed = json.loads(data)
            except (ValueError, TypeError, RecursionError):
                raise StreamError('PROVIDER_STREAM_INVALID') from None
            if not isinstance(parsed, dict):
                raise StreamError('PROVIDER_STREAM_INVALID')
            if self.inspect_event is not None:
                self.inspect_event(parsed)
            kind = parsed.get('type', event)
            if not isinstance(kind, str):
                raise StreamError('PROVIDER_STREAM_INVALID')
            if event and parsed.get('type', event) != event:
                raise StreamError('PROVIDER_STREAM_INVALID')
            if kind == 'error':
                raise StreamError('PROVIDER_STREAM_FAILED')
            if kind in {'response.output_item.added', 'response.output_item.done'}:
                item = parsed.get('item')
                if not isinstance(item, dict):
                    raise StreamError('PROVIDER_STREAM_INVALID')
                if item.get('type') not in {'reasoning', 'message'} and not (
                    self.allow_data_envelope and item.get('type') == 'function_call'
                    and item.get('name') == 'return_structured_result'):
                    raise StreamError('UNSUPPORTED_TOOL_CALL')
            if kind.startswith('response.function_call_arguments.') and not self.allow_data_envelope:
                raise StreamError('UNSUPPORTED_TOOL_CALL')
            if self.preview:
                self._public_event(kind, parsed)
            if kind in {'response.completed', 'response.incomplete', 'response.failed'}:
                response = parsed.get('response')
                expected = kind.split('.')[1]
                if not isinstance(response, dict) or response.get('status') != expected:
                    raise StreamError('PROVIDER_STREAM_INVALID')
                if self.preview and kind == 'response.completed':
                    output = response.get('output')
                    if not isinstance(output, list):
                        raise StreamError('PROVIDER_STREAM_INVALID')
                    for item in output:
                        if not isinstance(item, dict) or item.get('type') not in {'reasoning', 'message'}:
                            raise StreamError('UNSUPPORTED_TOOL_CALL')
                        if item.get('type') == 'message':
                            content = item.get('content')
                            if (item.get('role') != 'assistant' or item.get('status') not in {None, 'completed'}
                                    or not isinstance(content, list) or any(not isinstance(part, dict)
                                        or part.get('type') != 'output_text' or not isinstance(part.get('text'), str) for part in content)):
                                raise StreamError('UNSUPPORTED_RESPONSE_CONTENT')
                            if item.get('id') == self.public_item and self.preview.raw \
                                    and ''.join(part['text'] for part in content) != self.preview.raw:
                                self.preview.revoke()
                    if self.preview.raw and sum(item.get('id') == self.public_item for item in output) != 1:
                        self.preview.revoke()
                self.result = response

    def _public_event(self, kind, parsed):
        if kind in {'response.incomplete', 'response.failed', 'response.refusal.delta', 'response.refusal.done'}:
            self.preview.revoke()
        elif kind in {'response.output_item.added', 'response.output_item.done'}:
            item = parsed['item']
            if item.get('type') != 'message':
                return  # Reasoning/summary content is never forwarded.
            if item.get('role') != 'assistant' or item.get('phase') not in {None, 'final_answer'}:
                self.preview.revoke()
                return
            if not isinstance(item.get('id'), str) or not item['id'] or type(parsed.get('output_index')) is not int:
                self.preview.revoke()
                return
            content = item.get('content', [])
            if not isinstance(content, list) or any(not isinstance(part, dict) or part.get('type') != 'output_text' for part in content):
                # Preview is optional. Non-public/intermediate content must not
                # abort a valid final response (nor ever become visible text).
                # Terminal content, refusal and tool checks remain authoritative.
                self.preview.revoke()
                return
            if kind == 'response.output_item.added':
                if self.public_item is not None:
                    self.preview.revoke()
                else:
                    self.public_item, self.public_index = item['id'], parsed['output_index']
            elif item['id'] == self.public_item:
                if item.get('status') != 'completed' or len(content) != 1 or not isinstance(content[0].get('text'), str):
                    self.preview.revoke()
                    return
                text = content[0]['text']
                if self.preview.raw and self.preview.raw != text:
                    # Preserve the original full-response contract when the
                    # optional provisional text is revised by the provider.
                    self.preview.revoke()
                else:
                    self.preview.replace(text)
        elif kind == 'response.output_text.delta':
            if (self.public_item is None or parsed.get('item_id') != self.public_item
                    or parsed.get('output_index') != self.public_index or parsed.get('content_index') != 0):
                self.preview.revoke()
                return
            if not isinstance(parsed.get('delta'), str):
                raise StreamError('PROVIDER_STREAM_INVALID')
            self.preview.append(parsed['delta'])
            if any(secret in self.preview.raw for secret in self.preview.secrets):
                raise StreamError('PROVIDER_SECRET_ECHO')
        elif kind in {'response.content_part.added', 'response.content_part.done'}:
            part = parsed.get('part')
            if not isinstance(part, dict) or part.get('type') != 'output_text':
                self.preview.revoke()

    def finish(self):
        if self.result is not None:
            return self.result
        if self.preview:
            self.preview.revoke()
        try:
            self.buffer += self.decoder.decode(b'', final=True)
        except UnicodeError:
            raise StreamError('PROVIDER_STREAM_INVALID') from None
        # EOF must not synthesize an event terminator. Even valid JSON without
        # the SSE blank line is not a fully received terminal event.
        raise StreamError('PROVIDER_STREAM_INCOMPLETE')
