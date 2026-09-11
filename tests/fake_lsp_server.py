"""A language server that speaks just enough JSON-RPC for the client tests; run as a subprocess."""
import json
import os
import sys

FILE_URI = None
CONFIG_REPLY = None


def read():
    stream = sys.stdin.buffer
    header = stream.readline()
    while header and not header.lower().startswith(b"content-length:"):
        header = stream.readline()
    if not header:
        return None
    length = int(header.split(b":")[1])
    while stream.readline() not in (b"\r\n", b"\n", b""):
        pass
    return json.loads(stream.read(length))


def send(msg):
    body = json.dumps(msg).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def reply(rid, result):
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def rng(l1, c1, l2, c2):
    return {"start": {"line": l1, "character": c1}, "end": {"line": l2, "character": c2}}


def handle(msg):
    global FILE_URI, CONFIG_REPLY
    method = msg.get("method")
    rid = msg.get("id")
    if method is None and rid == 100:
        CONFIG_REPLY = msg.get("result")
        return
    if method is None and rid == 101:
        return
    if method == "initialize":
        reply(rid, {"capabilities": {}})
        send({"jsonrpc": "2.0", "id": 100, "method": "workspace/configuration",
              "params": {"items": [{"section": "a"}, {"section": "b"}]}})
        if os.environ.get("FAKE_INDEXING"):
            send({"jsonrpc": "2.0", "id": 101, "method": "window/workDoneProgress/create",
                  "params": {"token": "backgroundIndexProgress"}})
            send({"jsonrpc": "2.0", "method": "$/progress",
                  "params": {"token": "backgroundIndexProgress", "value": {"kind": "begin", "title": "indexing"}}})
    elif method == "test/indexed":
        send({"jsonrpc": "2.0", "method": "$/progress",
              "params": {"token": "backgroundIndexProgress", "value": {"kind": "end"}}})
        reply(rid, None)
    elif method == "textDocument/didOpen":
        FILE_URI = msg["params"]["textDocument"]["uri"]
    elif method == "textDocument/documentSymbol":
        reply(rid, [{"name": "C", "kind": 5, "detail": "", "range": rng(0, 0, 5, 0),
                     "selectionRange": rng(0, 6, 0, 7),
                     "children": [{"name": "m", "kind": 6, "detail": "(self) -> int",
                                   "range": rng(1, 4, 4, 0), "selectionRange": rng(1, 8, 1, 9)}]}])
    elif method == "textDocument/prepareCallHierarchy":
        reply(rid, [{"name": "m", "kind": 6, "uri": FILE_URI, "range": rng(1, 4, 4, 0),
                     "selectionRange": rng(1, 8, 1, 9)}])
    elif method == "callHierarchy/incomingCalls":
        reply(rid, [{"from": {"name": "caller", "kind": 12, "uri": FILE_URI, "range": rng(7, 0, 9, 0),
                              "selectionRange": rng(7, 4, 7, 10)}, "fromRanges": []}])
    elif method == "callHierarchy/outgoingCalls":
        if os.environ.get("FAKE_NO_OUTGOING"):
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})
        else:
            reply(rid, [])
    elif method == "workspace/symbol":
        reply(rid, [{"name": "m", "kind": 6, "containerName": "C",
                     "location": {"uri": FILE_URI, "range": rng(1, 4, 4, 0)}},
                    {"name": "other", "kind": 12}])
    elif method == "textDocument/references":
        reply(rid, [{"uri": FILE_URI, "range": rng(8, 4, 8, 5)}])
    elif method == "textDocument/definition":
        reply(rid, {"targetUri": FILE_URI, "targetRange": rng(1, 0, 4, 0),
                    "targetSelectionRange": rng(1, 8, 1, 9), "originSelectionRange": rng(8, 4, 8, 5)})
    elif method == "test/configReply":
        reply(rid, CONFIG_REPLY)
    elif method == "test/never":
        pass
    elif method == "test/unsupported":
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})
    elif method == "shutdown":
        reply(rid, None)
    elif method == "exit":
        sys.exit(0)


def main():
    while True:
        msg = read()
        if msg is None:
            return
        handle(msg)


if __name__ == "__main__":
    main()
