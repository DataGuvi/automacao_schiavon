"""spec-sharepoint-nome-com-e-comercial: '&' no caminho vai codificado na URL."""
from commons import sharepoint as sp


class _Resp:
    ok, status, headers = True, 200, {"content-type": "application/pdf"}

    def body(self):
        return b"pdf"


class _Ctx:
    def __init__(self):
        self.urls = []
        self.request = self

    def get(self, url, **_):
        self.urls.append(url)
        return _Resp()


def test_download_codifica_e_comercial(tmp_path):
    ctx = _Ctx()
    entry = {"type": "arquivo", "name": "v&b_30242__001.pdf",
             "web_url": "https://x.sharepoint.com/sites/Loja/Docs/v&b_30242__001.pdf",
             "server_relative_url": "/sites/Loja/Docs/v&b_30242__001.pdf"}
    baixados = sp.download_files(ctx, [entry], {"id": 2, "name": "Dr. Phillips"}, tmp_path)
    assert len(baixados) == 1
    assert "v%26b_30242" in ctx.urls[0] and "v&b" not in ctx.urls[0]
