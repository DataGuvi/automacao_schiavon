from __future__ import annotations

from enum import IntEnum, StrEnum

__all__ = [
    "Fluxo",                 # a que pipeline a etapa pertence
    "Etapa",                 # passo do pipeline + percentual -> processo.cod_etapa
    "StatusExecucao",        # onde o caso está              -> processo.cod_status
    "status_apos_concluir",  # o status depois de uma etapa OK
    "StatusConciliacao",     # veredito da comparação -> fat_conciliacao(_item).cod_status
    "EnvioCotacao",          # entrega do envio       -> fat_cotacao_envio.envio_status
]


# =============================================================================
# Etapa — o passo do pipeline e o quanto dele já andou
# =============================================================================

class Fluxo(StrEnum):
    """Qual dos três pipelines uma etapa percorre."""

    INVOICE = "invoice"   # a nota: coletar, ler, identificar, conciliar
    COTACAO = "cotacao"   # o ciclo semanal da planilha de preços
    COLETA = "coleta"     # a varredura do SharePoint


# O código da etapa é lido em duas partes: a DEZENA diz o fluxo, a UNIDADE diz
# a posição dentro dele. `LER = 12` é "fluxo 1 (invoice), segunda etapa".
_BASE_DO_FLUXO = 10


class Etapa(IntEnum):
    """Passo do pipeline. Gravado em `processo.cod_etapa`.

    A numeração não é arbitrária: `dezena * 10 + posicao` (ver
    `_BASE_DO_FLUXO`). É esse encaixe que deixa o percentual de execução sair
    de uma divisão — `posicao / total_de_etapas` —, sem tabela de apoio.
    """
    COLETAR = 11
    LER = 12
    IDENTIFICAR_FORNECEDOR = 13
    CONCILIAR_COTACAO = 14
    CONCILIAR_ERP = 15
    ABRIR_CICLO = 21
    PUBLICAR_PLANILHA = 22
    NOTIFICAR = 23
    AGUARDAR_RESPOSTA = 24
    IMPORTAR_PRECOS = 25
    NAVEGAR = 31
    BAIXAR = 32

    @property
    def fluxo(self) -> Fluxo:
        return _FLUXO_POR_DEZENA[self // _BASE_DO_FLUXO]

    @property
    def posicao(self) -> int:
        """Posição desta etapa dentro do fluxo dela (1 = a primeira)."""
        return self % _BASE_DO_FLUXO

    @property
    def total_de_etapas(self) -> int:
        """Quantas etapas o fluxo desta etapa tem ao todo."""
        return _TOTAL_DE_ETAPAS_POR_FLUXO[self.fluxo]

    @property
    def e_a_ultima(self) -> bool:
        """True quando concluir esta etapa fecha o caso."""
        return self.posicao == self.total_de_etapas

    @property
    def percentual_concluido(self) -> int:
        """Quanto do fluxo está pronto quando esta etapa termina BEM."""
        return self._percentual(etapas_prontas=self.posicao)

    @property
    def percentual_anterior(self) -> int:
        """Quanto do fluxo está pronto quando esta etapa FALHA.

        É o percentual da etapa anterior: a que falhou não conta como
        executada, então o caso não pode "andar" por ter dado erro.
        """
        return self._percentual(etapas_prontas=self.posicao - 1)

    def _percentual(self, etapas_prontas: int) -> int:
        return round(etapas_prontas * 100 / self.total_de_etapas)


_FLUXO_POR_DEZENA: dict[int, Fluxo] = {
    1: Fluxo.INVOICE,
    2: Fluxo.COTACAO,
    3: Fluxo.COLETA,
}

# Resolvido uma vez, na importação: a maior posição de um fluxo é o total de
# etapas dele. Antes isto era um `max()` varrendo o enum a cada chamada.
_TOTAL_DE_ETAPAS_POR_FLUXO: dict[Fluxo, int] = {
    fluxo: max(etapa.posicao for etapa in Etapa if etapa.fluxo is fluxo)
    for fluxo in Fluxo
}

# Execuções NOVAS (além da inicial) que o robô espera a PO virar 'Ordered'
# antes de reportar "nenhum PO Ordered". `processo.tentativas_po` conta a
# partir de 1 na execução inicial; ver `processo_service.aguardar_po_ordered`.
MAX_TENTATIVAS_PO = 3


class StatusExecucao(IntEnum):
    """Onde o caso está. Gravado em `processo.cod_status` (número) e
    `processo.status_exec` (nome).
    O código agrupa por faixa, e é pela faixa que o SQL filtra:

        0- 9  terminou
       10-19  em curso, ou parado à espera de algo externo
       20-29  encerrado sem completar, por falta de insumo
       50-59  erro técnico — a única faixa que volta pra fila de reprocesso
              (`cod_status BETWEEN 50 AND 59`)
    """

    descricao: str

    def __new__(cls, codigo: int, descricao: str) -> "StatusExecucao":
        membro = int.__new__(cls, codigo)
        membro._value_ = codigo
        membro.descricao = descricao
        return membro


    FINALIZADO = 0, "Todas as etapas concluídas."
    FINALIZADO_COM_ALERTA = 1, "Concluído, mas há item marcado para revisão."
    PENDENTE = 10, "Criado, nenhuma etapa executada ainda."
    EM_ANDAMENTO = 11, "Alguma etapa concluída, faltam outras."
    AGUARDANDO_RESPOSTA = 12, "Planilha enviada, fornecedor não preencheu."
    PO_NAO_ENCONTRADA = 13, "PO sem status Ordered no Catapult; pesquisar de novo na próxima execução."
    ENCERRADO_SEM_ARQUIVO = 21, "Pasta da semana encontrada, mas vazia."
    ERRO_LOGIN = 50, "Falha de autenticação na origem."
    ERRO_NAVEGACAO = 51, "Pasta ou arquivo não encontrado na origem."
    ERRO_LEITURA = 52, "A IA não conseguiu extrair o documento."
    ERRO_API = 53, "Falha de rede ou de serviço externo."
    ERRO_BAIXA_CONFIANCA = 54, "Leitura abaixo do piso de confiança."
    ERRO_SEM_FORNECEDOR = 55, "Nome da nota não casou com nenhum alias."
    REPROCESSAR_CONCILIACAO = 56, "Marcado para reconciliar de novo após correção de vocabulário/de-para."


def e_erro_tecnico(cod_status: int | None) -> bool:
    """`True` se `cod_status` esta na faixa de erro tecnico (50-59)."""
    return cod_status is not None and 50 <= int(cod_status) <= 59


def status_apos_concluir(etapa: Etapa, com_alerta: bool = False) -> StatusExecucao:
    """Status do caso depois que `etapa` termina bem.

    É aqui que "terminou" deixa de ser adivinhação: a última etapa do fluxo
    fecha o caso, qualquer outra deixa EM_ANDAMENTO. `com_alerta` só muda o
    desfecho de quem fecha — concluiu, mas ficou item para conferir.

    O percentual que acompanha este status sai da própria etapa
    (`percentual_concluido`); quando ela falha, de `percentual_anterior`.
    """
    if not etapa.e_a_ultima:
        return StatusExecucao.EM_ANDAMENTO
    return (StatusExecucao.FINALIZADO_COM_ALERTA if com_alerta
            else StatusExecucao.FINALIZADO)

class StatusConciliacao(IntEnum):
    """Resultado de comparar uma linha faturada com o PO do Catapult — hoje a
    ÚNICA comparação que existe (`comparacao='erp'`). Gravado em
    `fat_conciliacao(_item).cod_status`.

    Faixas:
        0- 9  bate
       10-19  diverge do PO
       20-29  não dá para comparar

    10 e 11 (acima/abaixo do cotado) estão aposentados junto com a comparação
    contra cotação — ver a nota no topo do módulo. Não reaproveitar os números.
    """
    descricao: str

    def __new__(cls, codigo: int, descricao: str) -> "StatusConciliacao":
        membro = int.__new__(cls, codigo)
        membro._value_ = codigo
        membro.descricao = descricao
        return membro

    CONFERIDO = 0, "Preço dentro da tolerância."
    DIVERGENCIA = 12, "Quantidade e/ou preço divergem do PO, além da tolerância."
    SEM_REFERENCIA_ITEM = 20, "Item da nota sem par no PO do Catapult."
    NAO_COMPARADO = 21, "Nota não comparada (exclusão intencional, ex.: insumo)."

class EnvioCotacao(StrEnum):
    """O que aconteceu com o envio da planilha a um fornecedor.

    Gravado em `fat_cotacao_envio.envio_status` — o valor é a string do
    membro. Antes disto o dict que o Twilio devolve trazia `status` e o código
    o descartava: falha de envio de WhatsApp só saía num `print`.
    """

    descricao: str

    def __new__(cls, valor: str, descricao: str) -> "EnvioCotacao":
        membro = str.__new__(cls, valor)
        membro._value_ = valor
        membro.descricao = descricao
        return membro

    ENFILEIRADO = "enfileirado", "Aceito pelo provedor, aguardando envio."
    ENVIADO = "enviado", "Provedor confirmou o envio."
    ENTREGUE = "entregue", "Entrega confirmada pelo destinatário."
    FALHOU = "falhou", "Erro no envio."
    SEM_CANAL = "sem_canal", "Fornecedor sem WhatsApp nem e-mail cadastrado."

    @staticmethod
    def do_twilio(status: str | None) -> "EnvioCotacao":
        """Traduz o status inicial do Twilio (queued/sent/accepted/...)."""
        recebido = (status or "").lower()
        if recebido == "delivered":
            return EnvioCotacao.ENTREGUE
        if recebido in ("failed", "undelivered", "canceled"):
            return EnvioCotacao.FALHOU
        if recebido in ("queued", "accepted", "scheduled"):
            return EnvioCotacao.ENFILEIRADO
        return EnvioCotacao.ENVIADO
