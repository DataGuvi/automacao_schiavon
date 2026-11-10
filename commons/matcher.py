"""Motor de match e comparacao - normalizacao, tolerancia e casamento de itens.

Este modulo NAO importa nada do projeto (so stdlib + rapidfuzz), de proposito:
assim ele e testavel sem Postgres e pode ser reaproveitado pela frente ERP. O
de-para de vocabulario de item (antes o dict `_SINONIMOS_ITEM`) entra por
parametro (`sinonimos`), carregado do banco pelo chamador.

===========================================================================
REGRAS DE NEGOCIO E DECISOES DE PROJETO
Toda a justificativa fica aqui; as funcoes abaixo levam so o resumo de uma
linha e uma referencia a secao.
===========================================================================

1. TOLERANCIA DE VALOR (`ABS_TOL`, `PCT_TOL`, `within_tolerance`)
   So e divergencia se estourar os DOIS limites: 1 centavo num item de 2 dolares
   e percentualmente grande mas irrelevante, e 0,3% num item de 500 dolares e o
   arredondamento de sempre. Faltando um dos lados, "nao da pra comparar" conta
   como NAO bate - quem chama decide o que fazer com o None.

2. PRECO POR LIBRA x POR CAIXA (`is_unit_priced`)
   A cotacao e sempre por LB. Na invoice de carne o `unit_price` tambem e por LB
   mesmo com `unit='CS'`: o total fecha com o peso, nao com a contagem de caixas
   (3 CS x 6.01 != 1256.09, mas 209.00 lb x 6.01 == 1256.09). Numa linha vendida
   por caixa/unidade, ao contrario, quantidade x preco fecha o total (1 x 85.90
   de uma caixa de alface) - essa linha nao e comparavel com preco por libra,
   dai `unit_mismatch`. A coluna `unit` nao serve pra isso: esta suja ('CASE',
   'case', 'CS', 'CX', '25#') e nula em quase 10% dos itens.
   A cotacao as vezes traz uma FAIXA ('$4.95/5.35/$5.41'); a faixa inteira conta
   como dentro do combinado (`parse_price_range`). Convencao do projeto:
   `diff = invoice - cotacao` (positivo = cobraram a mais).

3. VOCABULARIO DE ITEM (`norm_item`, parametro `sinonimos`)
   `norm_text` sozinho nao basta porque os dois lados falam linguas diferentes
   sobre o mesmo produto. Medido nos 4 itens reais em base, so 1 dos 4 pares
   corretos alcancava o piso antigo de 60; com os dois passos abaixo os 4 passam
   de 88, com margem de +41 a +58 sobre o segundo colocado.
   - Passo 1, cauda de catalogo: ' - AMICK, Item #227059' (invoice) e ' - 40622'
     (cotacao) sao estrutura, nao vocabulario. Do outro lado nao existem, entao
     so diluem o score.
   - Passo 2, de-para `sinonimos` ({token -> termo alvo}): abreviacao de mercado,
     traducao PT/EN, sinonimo de corte. Era um dict hardcoded aqui; virou dado de
     banco (dwschiavon2.dim_item_sinonimo), alimentado pela aba De-Para de uma
     planilha do Google Sheets (conciliacao/sinonimos.py; entradas iniciais
     migradas em manutencao/migrar_sinonimos_sheets.py) e injetado por parametro
     pra nao quebrar a regra de este modulo nao importar nada do projeto.
     A FRASE inteira e trocada ANTES dos tokens, senao 'CHIX TENDER' viraria
     'CHICKEN TENDER' e nao casaria com a frase cadastrada. Aplicado aos DOIS
     lados. `sinonimos=None` degrada com seguranca: sem o de-para o unico token
     em comum entre 'CHIX TENDER JUMBO CVP' e 'CHICKEN - Tender / Sassami de
     Frango' e 'TENDER' e o par correto faz 46 - errar pra menos e seguro, nunca
     casa produto errado.
   O resultado e texto de COMPARACAO: nunca exibir ao usuario.

4. PISOS DE SIMILARIDADE (`EXACT_MIN_SCORE`, `FUZZY_THRESHOLD`, `CONFIDENT_SCORE`)
   Os tres numeros foram RECALIBRADOS junto com `norm_item`. Antes dele a
   comparacao era feita sobre o texto cru, onde marca, codigo de catalogo e nome
   em portugues diluiam o score; os pisos antigos (60/50/70) refletiam essa nuvem
   achatada - o comentario original registrava "o melhor par CORRETO observado
   faz 69". Com `norm_item`, medido sobre os 4 pares corretos e 5 pares errados
   que existem em base, as duas nuvens se separam:
       pares corretos   88.0 .. 91.9
       pares errados    24.6 .. 50.0   (o pior e Chuck Roll x Knuckle, 50.0)
   Vao de 38 pontos; os pisos ficam dentro dele, longe das duas bordas.
   ATENCAO ao mexer: sao 9 pares medidos, amostra pequena. Subir demais volta a
   perder par correto em silencio (sai como 'no_quote_for_item'); descer demais
   ressuscita o falso positivo por preco que o teste
   `test_preco_igual_nao_basta_para_casar_produtos_sem_relacao` guarda. Qualquer
   mudanca aqui deve ser medida de novo, nao estimada.
   - `EXACT_MIN_SCORE` (65): Etapa 1 de `match_items`, onde o preco ja bate e a
     descricao so precisa dar apoio minimo. Sem este piso, dois produtos sem
     relacao casam so por custarem o mesmo: "Beef, Chuck Roll 'Neck Off'
     Boneless" casou com "BEEF - Knuckle / Patinho" porque ambos custavam 4.99 e
     era o unico candidato.
   - `FUZZY_THRESHOLD` (70): Etapa 2, onde a descricao decide SOZINHA, sem apoio
     do preco - piso mais alto que o da Etapa 1 justamente por isso.
     `token_set_ratio` no lugar de `token_sort_ratio` porque a invoice carrega
     marca e pack size a mais, e o sort_ratio pune diferenca de tamanho.
   - `CONFIDENT_SCORE` (70): acima do piso mas abaixo disto o par se sustenta em
     pouca evidencia textual. Aceita, mas marca `ambiguous` pra conferencia
     humana: se o produto estiver errado, a divergencia verdadeira passa
     despercebida como "coerente". Mantido em 70 de proposito - o corpus nao
     sustenta valor mais alto: tirando os pares que `grades_conflict` ja barra
     antes do score, o pior par CORRETO faz 88.0 e o pior par ERRADO faz 86.8
     ('CHICKEN Breast Boneless' x 'CHICKEN Thighs Boneless', que so diferem no
     corte). As nuvens quase se tocam, entao nao existe fronteira de confianca
     defensavel nessa faixa - quem separa esse par e a atribuicao 1-para-1
     gulosa da Etapa 2, nao o piso.

5. DESEMPATE PELO SCORE SIMETRICO (`token_sort_ratio`)
   `token_set_ratio` devolve EXATAMENTE 100 sempre que os tokens de um lado sao
   subconjunto do outro. O nome do Catapult/cotacao e quase sempre mais curto que
   a descricao da invoice, entao o token que DISTINGUE dois produtos parecidos
   (sem acucar, sabor, tamanho, corte, cor) sai de graca quando aparece de um
   lado so, e varias linhas da invoice empatam em 100 contra o mesmo nome.
   Enquanto o empate era resolvido pela ORDEM das linhas, quem decidia o par nao
   era a descricao. Caso real Restaurant Depot/21460631281488440 (nota sem
   divergencia nenhuma):
                                          token_set   token_sort
       inv 'Red Bull ... Sugar Free' x PO 'Energy Drink Red Bull'
                                             100.00        66.67  <- vencia, ERRADO
       inv 'Red Bull ... Sugar Free' x PO '... Red Bull Sugar free'
                                             100.00        86.49  <- par certo
       inv 'Red Bull Energy Drink'   x PO 'Energy Drink Red Bull'
                                             100.00        80.77  <- par certo
       inv 'Red Bull Energy Drink'   x PO '... Red Bull Sugar free'
                                              80.77        69.84
   A linha do Sugar Free chegou primeiro, levou o PO normal e sobrou pro normal o
   PO Sugar free: duas divergencias de preco que nao existem, as duas sem
   `revisar` (100 e 80.77 passam do CONFIDENT_SCORE). `token_sort_ratio` e o
   sinal que faltava - pune token sobrando em QUALQUER um dos dois lados - e
   separa os tres empates acima.
   Ele entra SO como critetio de desempate, nunca como piso nem misturado ao
   score principal, porque em par CORRETO ele e baixo (a invoice carrega marca,
   pack size e codigo que o outro lado nao tem): medido nos pares corretos em
   base, 'BEEF PEELED KNUCKLE 3230...' x 'BEEF KNUCKLE KNUCKLE' faz set 100.00 /
   sort 53.57, e 'CHICKEN TENDER JUMBO CVP' x 'CHICKEN TENDER TENDER DE CHICKEN'
   faz set 90.32 / sort 60.71. Piso simetrico derrubaria par certo em silencio.
   Ordem de atribuicao: `(-score, -score_simetrico, i, j)` - a ordem das linhas
   fica como ultimo critetio, so pra o resultado ser deterministico. Empate que o
   simetrico tambem nao resolve (dois candidatos com os DOIS scores iguais) sai
   com `ambiguous`: nada na descricao distingue os dois. Ver
   `_atribuir_guloso`, a atribuicao das duas frentes.

6. GRADES DE CARNE (`grades_in`, `grades_conflict`)
   CHOICE e SELECT sao cortes diferentes com precos diferentes; casa-los
   produziria uma divergencia que nao existe. Dois lados citando grades sem nada
   em comum nunca casam, em nenhuma etapa.

7. FRESCO x CONGELADO NA MESMA INVOICE (`mesmo_item_estado_diferente`)
   Caso real Prime Meats/1175605 (achado do cliente, Leandro Rocha): 'CHKN BREAST
   BL/SL DRY GEN FZN' e 'CHKN BREAST BL/SL UNSIZED DRY GEN FRSH CVP' sao o mesmo
   peito de frango pra quem recebe - so uma parte do pedido veio congelada e a
   outra fresca. Estado e forma de entrega, nao identidade do produto, entao as
   palavras de estado (`_PALAVRAS_ESTADO`) ficam FORA do de-para de sinonimos de
   proposito: isto e regra de agrupar duas linhas da MESMA invoice, nao traducao
   de vocabulario compartilhada com reconcile_quote.py.
   Removidas as palavras de estado, `token_set_ratio` tolera bem token extra de
   UM lado so, que e exatamente o formato do par acima: o lado congelado (sem
   UNSIZED/CVP) fica um SUBCONJUNTO exato do lado fresco e pontua 100.
   Justamente por aceitar subconjunto, a funcao EXIGE que as palavras de estado
   dos dois lados sejam DIFERENTES - e a premissa dela ("o mesmo item, so o
   estado muda"). Sem essa exigencia, duas linhas que nao falam de estado nenhum
   passavam por variante de estado: as duas linhas de Red Bull da nota
   21460631281488440 ('... Sugar Free ...' e '... Energy Drink ...') dao 100
   depois do corte e seriam SOMADAS contra uma unica linha de PO por
   `conciliacao/reconcile_erp.py::_herdar_po_de_irma_estado`. Dois lados sem
   palavra de estado, ou com o mesmo estado, nao fundem.
   `MESMO_ITEM_ESTADO_DIFERENTE_SCORE` (95) e alto de proposito: aqui a acao e
   SOMAR duas linhas num grupo pra comparar com o PO, mais consequente que so
   marcar "precisa revisao", entao pede muito mais evidencia textual que o
   `CONFIDENT_SCORE` (70). Grade conflitante bloqueia - nunca funde CHOICE com
   SELECT por coincidencia de score. Usado so pra decidir se duas linhas da
   invoice sao a MESMA linha de PO, nao pra casar contra PO/cotacao (la o estado
   pode ser informativo) - ver `conciliacao/reconcile_erp.py`.

8. NOME DE FORNECEDOR (`match_supplier`, `pontuar_nome_catapult`,
   `aceitar_prefixos_catapult`)
   `token_set_ratio` porque a razao social da invoice costuma ser um
   superconjunto do nome curto do cadastro ('CHENEY BROTHERS, INC.' / 'Cheney').
   `match_supplier` serve so pra SUGERIR de-para pra revisao humana, nunca pra
   decidir sozinho: 'Prime Meats' (carne) e 'Prime Distribution USA' (mercearia)
   pontuam alto e sao fornecedores diferentes. Candidato de 1-2 letras ('FL') e
   subconjunto de qualquer razao social longa ('FRESHPOINT CENTRAL FL') e
   pontuaria 100, entao nome curto (`_MIN_CHARS_CANDIDATO_SUPPLIER`) so vale se
   for IGUAL ao da nota (o proprio fornecedor 'FL').
   No Catapult a busca e por PREFIXO do campo Name do PO. `pontuar_nome_catapult`
   da 100 quando os tokens de um lado estao contidos nos do outro ('Leblon foods'
   x 'Leblon'): palavra a mais ou a menos nao e diferenca, DESDE QUE o conjunto
   menor tenha um token que identifique de fato - 'Sysco Foods' x 'Foods' nao
   casa por subconjunto (`_TOKENS_GENERICOS`). Senao vale o melhor entre
   `token_sort_ratio` e a razao dos textos sem espaco (cobre 'Fresh Poin' x
   'Freshpoint'). Um token em comum entre nomes de tokens distintos ('Prime
   Meats' x 'Prime Distribution') NAO chega ao piso `_PISO_NOME_CATAPULT` (85).
   Prefixo IGUAL (normalizado) a um dos nomes tem precedencia: havendo igualdade,
   so os iguais valem ('Prime' nao disputa com 'Prime Meats'). Mais de um aceito
   sem igualdade = ambiguo: quem chama desempata por itens e valor, NUNCA pelo
   score.

9. CODIGO DE ITEM (`norm_code`)
   Compara item_code/upc da invoice com Supplier Unit ID/scancode do PO, que
   formatam o mesmo codigo de jeitos diferentes. Duas correcoes de bug moram
   aqui (spec-codigo-na-nao-casa):
   - 'N/A' disfarcado de codigo (`_SEM_CODIGO_RE`): 'NA-0001', 'N/A 0007', 'na' e
     o jeito de escrever "esta linha nao tem codigo", nao um codigo de catalogo.
     Sem o guard, a antiga normalizacao tirava as letras e sobrava um digito curto
     ('1'), que colide com o Supplier Unit ID de um item SEM NENHUMA RELACAO no
     mesmo PO (achado real, invoice 1104: 'NA-0001' casou por codigo com 'FEIJAO
     CARIOCA GALANTE 1KG' so porque os dois normalizavam pra '1'). So pega 'N'+'A'
     juntos (separador opcional) seguido so de digitos, entao nao afeta codigo
     alfanumerico de verdade ('TOR-0003', 'NAT12345').
   - so separador de FORMATACAO sai (`_SEPARADOR_RE`), nunca letra: o prefixo
     alfanumerico e parte da IDENTIDADE do codigo. Achado real (fornecedor de
     salgadinhos) - a antiga regex (`\\D+`, tirava tudo que nao e digito) reduzia
     'TOR-0003', 'BIS-0003', '0003' e '3' pro MESMO '3', e o primeiro desses
     codigos que aparecesse no PO blindava os outros no indice (`setdefault`),
     entao o item certo nunca era alcancado pelo proprio codigo.
   Zero a esquerda SO cai quando o que sobra e puramente numerico (EAN/UPC com
   padding diferente nos dois lados, '00-078900-12345' x '78900-12345'); codigo
   com letra mantem os zeros ('TOR0003' nao vira 'TOR3'). Vazio (nenhum digito,
   so zeros, ou 'N/A') nunca casa com nada, nem com outro vazio.

10. MATCH CONTRA A COTACAO (`match_items`)
   Etapa 1 - assinatura de preco. Casa o que e facil e exato. Uma linha da
   cotacao e uma tabela de preco, entao pode servir a varias linhas da invoice
   (N-para-1 e legitimo aqui).
   Etapa 2 - nome do item, so sobre o residuo. Por construcao, tudo que sobrou da
   Etapa 1 tem preco que nao bate com nenhuma cotacao - ou seja, e exatamente
   onde estao as divergencias. Guloso e 1-para-1.
   Etapa 3 - o que sobrou fica 'unmatched'.
   Por que o preco nao pode ser chave unica do par: se o par so existisse quando o
   preco bate, todo item faturado com preco errado sairia como "sem cotacao" e a
   divergencia que se quer achar ficaria invisivel.

11. MATCH CONTRA O PO DO CATAPULT (`match_items_po`)
   Cascata diferente da de `match_items`: la o preco decide primeiro porque a
   invoice nao carrega codigo de produto; aqui o PO tem Supplier Unit ID /
   scancode, entao o CODIGO decide primeiro e o preco so entra DEPOIS do match,
   como um dos dois pontos comparados (o outro e quantidade) - nunca pra achar o
   par. O PO nunca tem linha duplicada (garantia de quem popula `po_lines`), entao
   um codigo normalizado aponta pra no maximo um item: ao contrario de
   `match_items`, aqui nao existe N-para-1 legitimo entre codigos distintos.
   Etapa 1 - codigo. `item_code`/`upc`/`handwritten_code` nessa ordem; o primeiro
   que bater ganha. `handwritten_code` entra por ultimo de proposito: quando o
   codigo IMPRESSO bate ele e mais confiavel; quando nao bate com nada (caso
   Cheney Brothers - o impresso e catalogo do fornecedor, nao do Catapult), o
   numero escrito a mao e a ultima tentativa antes do nome. So "cola" se bater de
   verdade com um codigo real do PO, entao em fornecedor onde a anotacao a mao e
   outra coisa (codigo da cotacao semanal, por exemplo) a linha simplesmente segue
   pro residuo, sem risco de casar errado. Invoice sem nenhum desses codigos cai
   direto no residuo. Sem checagem de linha ja reclamada nesta etapa, de
   proposito: duas linhas da invoice com o MESMO codigo (produto faturado em duas
   linhas) casam as duas com a mesma PO e se somam depois em `_comparar_grupo`.
   Linha de PO "zerada" (`_po_zerado`) NAO conta como match por codigo aqui: ela
   nunca foi de fato recebida/faturada neste PO, so existe na grade porque o
   Catapult lista o catalogo inteiro do fornecedor (ou um cadastro alternativo do
   mesmo item). Aceitar esse "match" rouba a linha certa (que so seria achada por
   nome) e deixa `valor_po`/`qtd_po` com zero em vez do valor real. Dois casos
   reais: MENA/11126, Sococo Agua de Coco (codigo impresso colidiu com o Supplier
   Unit ID de um item do catalogo sem nenhuma atividade); Restaurant Depot/
   21147023139465080, Nestle Pure Life 40x0.5L (o item_code bateu com o cadastro
   do produto vendido A UNIDADE, `ordered`=160 mas `received`/
   `invoiced_total_cost`=0 - o PACK de 40 unidades que a invoice de fato vendia
   estava numa linha de PO SEPARADA, so achada por nome depois deste fix).
   Etapa 2 - nome, fuzzy (`norm_item` + `token_set_ratio`) sobre o residuo, so
   contra PO ainda nao reclamado E nao zerado. O filtro de zerada repete aqui
   porque rejeitar so na Etapa 1 nao bastava: a linha decoy pode ter nome
   textualmente MAIS parecido com a invoice do que a linha certa (no caso
   Restaurant Depot, 'Agua Pure Life' bate melhor com "Nestle Pure Life -
   Purified Water - 40/0.5L" do que 'Pack Agua 40un Nestle Pure Life 500ml', e e
   a linha errada). Tenta o melhor score entre `item_name` e `receipt_alias` de
   cada POLine - a invoice costuma falar mais perto do `receipt_alias` (grafia
   curta, de recibo) do que do `item_name` de cadastro. Guloso e 1-para-1: maior
   score fica com o par, o resto tenta o proximo candidato acima do limiar.
   Etapa 2b - codigo contra linha ZERADA, ultimo recurso
   (spec-po-zerado-ultimo-recurso). O codigo bate mas a linha nao foi recebida/
   faturada neste PO: o item da invoice existe e a divergencia (valor/quantidade
   contra $0) e justamente o que se quer mostrar (caso Gominas 5114, item 2914:
   PO com Ordered=24/Received=0/$0.00 e invoice cobrando $34.00 no mesmo codigo).
   Aqui a linha ja reclamada JA conta: uma PO zerada so e dada pra uma linha, e a
   segunda que pedir o mesmo codigo fica sem par.
   Etapa 2c - valor (spec-casamento-por-valor). Sem codigo nem nome: linha de PO
   livre com `invoiced_total_cost` igual ao valor da invoice; varias, desempata por
   `valor / received` x `ordered_unit_cost`; empate fica sem par.
   Etapa 3 - o que sobrou fica 'unmatched'.
   O "reverse check" do fluxograma (item recebido/pedido mas nao faturado) e
   `po_items_sem_invoice`.

12. CAMPOS DO PO E DA INVOICE (`POLine`, `InvoiceCodeLine`)
   `receipt_alias` vem na PROPRIA linha do PO (coluna "Receipt Alias" da grade) -
   confirmado contra o ambiente real, nao precisa de ponte por
   `dim_item_catapult`. `invoiced_total_cost` e o valor TOTAL da linha, nao preco
   unitario. `invoiced_unit_cost` e o custo por UNIDADE que o proprio Catapult
   carrega na grade (coluna "Invoiced Unit Cost") - usado so na secao 13.
   `ordered_unit_cost` (coluna "Ordered Unit Cost") e o mesmo papel, mas pra
   carne: essa categoria nao tem a coluna "Invoiced Unit Cost" na grade real
   (confirmado ao vivo, PO Eastern-008922-RS2) - a secao 13 usa este campo no
   lugar daquele so quando `categoria_fornecedor == "carne"`.
   `ordered` e `received` NAO estao sempre na mesma unidade um do outro: pra item
   de peso variavel (acougue) o Catapult registra `ordered` em caixas (o que se
   pede) e `received` em peso (o que se pesa na doca) - confirmado contra o
   Catapult real, caso Cheney Brothers. Essa diferenca de unidade nao importa mais
   pra quantidade (secao 13 nao usa `received`), so fica registrada aqui porque os
   dois campos continuam saindo em `qty_po`/`qty_po_received` (auditoria).
   `_po_zerado` exige os DOIS campos (`received` e `invoiced_total_cost`)
   CONHECIDOS e zero. `ordered` sozinho nao prova atividade real - e so o que foi
   pedido, nao o que chegou/foi cobrado. E quando os campos faltam (fixture de
   teste, raspagem que nao capturou a celula), a falta de dado nao e prova de que
   a linha e zerada.
   `handwritten_code` e candidato de codigo igual a `item_code`/`upc`, nao um
   terceiro tipo - ver secao 11.

13. QUANTIDADE, CASCATA DE CUSTO (spec-qtd-mismatch-custo-unitario)
   (Substitui a comparacao three-way `invoice x Ordered x Received` e todo o
   roteamento caixa/peca por `POLine.unit` - removidos.) O Catapult e a fonte de
   verdade; a LEITURA da invoice (quantidade/pecas) e o lado que mais erra
   (Vision, pack embutido na descricao, fornecedor sem PO previo), por isso a
   checagem nunca olha pra `quantity`/`cases` sozinha, so pro VALOR faturado.
   Os dois passos moram em `conciliacao/reconcile_erp.py::_comparar_grupo` e
   usam so `compare_price` e `within_tolerance` daqui - nao ha funcao propria:
   1. Valor total faturado do grupo x `po.invoiced_total_cost`. Se nao bate,
      PRICE_MISMATCH_PO ja acusa e o passo 2 nem roda - sem isso, duas issues
      nasceriam do MESMO numero errado.
   2. So se o passo 1 bateu: `valor_total / po.received` x
      `po.invoiced_unit_cost` (carne: `po.ordered_unit_cost` - ver abaixo).
      Dado ausente pula o passo sem marcar nada. `Received`, nao `Ordered` -
      medido ao vivo (PO Rest Depot-037333-HQ-RS2) que `Invoiced Unit Cost`
      do proprio Catapult E `Invoiced Total Cost / Received` (confirmado
      pelas linhas com `Ordered=0`, onde a divisao por `Ordered` e impossivel
      e o numero so pode vir de `Received`).
   Risco aceito: como `po.invoiced_unit_cost` e, por definicao do Catapult,
   `po.invoiced_total_cost / po.received`, o passo 2 NUNCA discorda de si mesmo
   quando o passo 1 ja bateu - fica inerte na pratica, nunca gera
   QTY_MISMATCH_PO sozinho. Decisao do usuario: aceitar, porque a leitura de
   quantidade da invoice e menos confiavel que o dado do Catapult.
   Carne (`categoria_fornecedor == "carne"`) nao tem a coluna "Invoiced Unit
   Cost" na grade (confirmado ao vivo, PO Eastern-008922-RS2) - o passo 2 usa
   `po.ordered_unit_cost` ("Ordered Unit Cost") no lugar, medido igual a
   `Invoiced Total Cost / Received` nas linhas reais (Cupim, Coxao Duro,
   Costelinha). Mesma formula, mesmo fail-safe quando ausente - MAS com a
   tolerancia PADRAO do motor (nao a tolerancia zero de preco de carne):
   "Ordered Unit Cost" nao e identidade matematica exata como "Invoiced Unit
   Cost", carrega ruido de arredondamento real (medido: diff de 0.000065 no
   Coxao Duro) que zero tolerancia reprovaria sempre.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, NamedTuple, Sequence

from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# Constantes. A justificativa de cada numero esta na secao indicada do topo.
# ---------------------------------------------------------------------------

# Tolerancia padrao de valor (secao 1).
ABS_TOL = Decimal("0.01")
PCT_TOL = Decimal("0.005")

# Pisos de similaridade de item (secao 4). Medidos, nao estimados.
EXACT_MIN_SCORE = 65.0
FUZZY_THRESHOLD = 70.0
CONFIDENT_SCORE = 70.0

# Piso mais baixo pra PO com codigo placeholder (spec-codigo-placeholder-casa-por-nome).
# Medido pelo caso real (Smirnoff Ice, Florida Distributing): o par correto faz
# 69.70, por uma fracao abaixo do FUZZY_THRESHOLD padrao - sem nenhum codigo pra
# apoiar (a PO nunca tem candidato de verdade), exigir o mesmo piso de quando o
# codigo some a invoice inteira. So entra em pares onde o lado do PO tem codigo
# placeholder (R1) - resto do sistema usa FUZZY_THRESHOLD, inalterado.
PLACEHOLDER_MIN_SCORE = 65.0

# Piso pra fundir duas linhas da MESMA invoice por estado diferente (secao 7).
MESMO_ITEM_ESTADO_DIFERENTE_SCORE = 95.0

# Pisos de nome de fornecedor (secao 8).
_PISO_NOME_CATAPULT = 85.0
_MIN_CHARS_CANDIDATO_SUPPLIER = 3

# Sufixos societarios removidos ao normalizar nome de fornecedor.
_COMPANY_SUFFIXES = frozenset({
    "INC", "LLC", "LLP", "LP", "CORP", "CORPORATION", "CO", "COMPANY",
    "LTD", "LTDA", "SA", "SL", "GROUP", "USA",
})

# Palavras que nao identificam o fornecedor: sozinhas nao bastam pro subconjunto
# de tokens valer 100 (secao 8).
_TOKENS_GENERICOS = frozenset({
    "food", "foods", "distribution", "distributor", "distributors", "distributing",
    "supply", "supplies", "wholesale", "imports", "import", "usa", "us", "trading",
    "group", "company", "co", "international", "products", "produce",
})

# Grades de carne que nunca podem casar entre si (secao 6).
_GRADES = ("CHOICE", "SELECT", "PRIME", "ANGUS", "WAGYU")

# Fresco x congelado: entrega, nao identidade do produto (secao 7).
_PALAVRAS_ESTADO = frozenset({"FRESH", "FRSH", "FROZEN", "FRZN", "FZN", "FRZ"})

# Rotulos de cabecalho que vazaram da planilha para dentro dos dados.
HEADER_LABELS = frozenset({"ITEM", "PRICE", "COMMENTS", "PRECO", "PRODUTO"})

# Caudas de catalogo cortadas antes de comparar descricao (secao 3): a da invoice
# (' - MARCA, Item #NNNNN') e a da cotacao (' - 40622').
_CAUDA_INVOICE_RE = re.compile(r"\s*-\s*[^,]+,\s*ITEM\s*#?\s*\d+\s*$", re.IGNORECASE)
_CAUDA_COTACAO_RE = re.compile(r"\s*-\s*\d{4,6}\s*$")

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_ITEM_CODE_RE = re.compile(r"-\s*(\d{4,6})\s*$")

# 'N/A' disfarcado de codigo e separador de formatacao dentro do codigo (secao 9).
_SEM_CODIGO_RE = re.compile(r"^N\s*[./-]?\s*A\s*[-\s]*\d*$", re.IGNORECASE)
_SEPARADOR_RE = re.compile(r"[\s./-]+")


# ---------------------------------------------------------------------------
# Normalizacao
# ---------------------------------------------------------------------------

def norm_text(value: str | None) -> str:
    """Maiusculas, sem acento, sem pontuacao, espacos colapsados (trata o espaco
    nao-separavel U+00A0 que vem dos nomes do Excel)."""
    if not value:
        return ""
    texto = unicodedata.normalize("NFKD", str(value))
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    texto = texto.replace("\xa0", " ").upper()
    texto = re.sub(r"[^0-9A-Z]+", " ", texto)
    return " ".join(texto.split())


def norm_supplier(value: str | None) -> str:
    """`norm_text` + remocao dos sufixos societarios (INC, LLC, CORP...)."""
    tokens = [token for token in norm_text(value).split() if token not in _COMPANY_SUFFIXES]
    return " ".join(tokens)


def norm_item(value: str | None, sinonimos: Mapping[str, str] | None = None) -> str:
    """Normaliza descricao de item para COMPARACAO entre os dois lados: corta a
    cauda de catalogo e aplica o de-para de vocabulario (secao 3).

    A frase do De-Para (DE com 2+ palavras) casa em QUALQUER posicao da
    descricao, nao so quando e a descricao inteira (spec-depara-frase-no-meio-
    da-descricao): em cada posicao, tenta o candidato de MAIS palavras
    primeiro, descendo até 1 palavra. Casou uma frase de N palavras, avanca N
    tokens — o resultado nao reencadeia com outra entrada do De-Para."""
    de_para = sinonimos or {}
    sem_cauda = _CAUDA_INVOICE_RE.sub("", value or "")
    normalizado = norm_text(_CAUDA_COTACAO_RE.sub("", sem_cauda))
    tokens = normalizado.split()
    if not tokens:
        return normalizado
    max_palavras = max((len(chave.split()) for chave in de_para), default=1)
    resultado = []
    i, n = 0, len(tokens)
    while i < n:
        for tamanho in range(min(max_palavras, n - i), 0, -1):
            candidato = " ".join(tokens[i:i + tamanho])
            if candidato in de_para:
                resultado.append(de_para[candidato])
                i += tamanho
                break
        else:
            resultado.append(tokens[i])
            i += 1
    return " ".join(resultado)


def is_header_row(item_name: str | None) -> bool:
    """True para a linha de cabecalho da planilha que virou dado (item_name='ITEM')."""
    return norm_text(item_name) in HEADER_LABELS


def extract_item_code(item_name: str | None) -> str | None:
    """Codigo numerico ao final do nome do item ('... - 40122' -> '40122')."""
    if not item_name:
        return None
    achado = _ITEM_CODE_RE.search(str(item_name).replace("\xa0", " ").strip())
    return achado.group(1) if achado else None


def norm_code(value: str | None) -> str:
    """Codigo de item comparavel entre invoice e PO: tira separador de formatacao
    (nunca letra) e, se o resto e puramente numerico, o zero a esquerda (secao 9)."""
    if not value:
        return ""
    texto = str(value).strip()
    if _SEM_CODIGO_RE.match(texto):
        return ""
    sem_separador = _SEPARADOR_RE.sub("", texto).upper()
    if not any(ch.isdigit() for ch in sem_separador):
        return ""
    if sem_separador.isdigit():
        return sem_separador.lstrip("0")
    return sem_separador


def norm_code_so_digitos(value: str | None) -> str:
    """Fallback de `norm_code` (secao 9, spec-codigo-na-nao-casa R7-R9): so
    digitos, sem a letra do prefixo. Existe pro caso OPOSTO ao que `norm_code`
    corrige — quando a letra preservada e um prefixo de MARCA colado pela
    Vision, nao parte da identidade do codigo ('Elvan-1200711111' x Supplier
    Unit ID '1200711111', sem o prefixo, achado real invoice Skyharvest
    13019). So deve ser tentado quando `norm_code` (com letra) nao bate com
    nada no PO, NUNCA como primeira tentativa nem para construir o indice do
    PO — senao reabre a colisao que `norm_code` corrige ('TOR-0003' x
    'BIS-0003' continuam distintos: a chave do PO no indice nunca vira so
    digito)."""
    if not value:
        return ""
    texto = str(value).strip()
    if _SEM_CODIGO_RE.match(texto):
        return ""
    return re.sub(r"\D+", "", texto).lstrip("0")


# ---------------------------------------------------------------------------
# Preco e valor da linha (secoes 1 e 2)
# ---------------------------------------------------------------------------

class PriceVerdict(NamedTuple):
    status: str                  # 'ok' | 'above' | 'below'
    diff: Decimal | None         # invoice - cotacao
    diff_pct: Decimal | None     # em %


def parse_price_range(price_raw: str | None) -> tuple[Decimal, Decimal] | None:
    """Faixa de preco a partir do texto original da celula.

    '$4.95/5.35/$5.41' -> (4.95, 5.41); '$5,38' -> (5.38, 5.38); 'N/A' -> None.
    """
    if price_raw is None:
        return None
    limpo = str(price_raw).strip().replace("$", "").replace(",", ".")
    if not limpo or limpo.upper() in ("N/A", "NA", "-"):
        return None
    valores: list[Decimal] = []
    for numero in _NUMBER_RE.findall(limpo):
        try:
            valores.append(Decimal(numero))
        except InvalidOperation:
            continue
    if not valores:
        return None
    return min(valores), max(valores)


def within_tolerance(
    valor: Decimal | float | None,
    referencia: Decimal | float | None,
    abs_tol: Decimal = ABS_TOL,
    pct_tol: Decimal = PCT_TOL,
) -> bool:
    """True se os dois valores sao considerados iguais: so diverge se estourar os
    DOIS limites (secao 1). O percentual e relativo a `referencia`."""
    if valor is None or referencia is None:
        return False
    valor, referencia = Decimal(str(valor)), Decimal(str(referencia))
    diferenca = abs(valor - referencia)
    if diferenca <= abs_tol:
        return True
    if referencia == 0:
        return False
    return (diferenca / abs(referencia)) <= pct_tol


def is_unit_priced(
    quantity: Decimal | float | None,
    unit_price: Decimal | float | None,
    total_price: Decimal | float | None,
    rel_tol: Decimal = Decimal("0.01"),
) -> bool:
    """True se a linha esta precificada por unidade/caixa (quantidade x preco
    fecha o total), e nao por libra (secao 2)."""
    if quantity is None or unit_price is None or total_price is None:
        return False
    quantidade = Decimal(str(quantity))
    preco_unitario = Decimal(str(unit_price))
    total_linha = Decimal(str(total_price))
    if total_linha == 0:
        return False
    total_esperado = quantidade * preco_unitario
    return abs(total_esperado - total_linha) <= abs(total_linha) * rel_tol


def compare_price(
    invoice_price: Decimal | float | None,
    quote_price: Decimal | float | None,
    price_raw: str | None = None,
    abs_tol: Decimal = ABS_TOL,
    pct_tol: Decimal = PCT_TOL,
) -> PriceVerdict:
    """Compara o preco faturado com o cotado, respeitando faixa e tolerancia
    (secoes 1 e 2). `diff = invoice - cotacao`; o percentual e sobre a cotacao."""
    if invoice_price is None or quote_price is None:
        return PriceVerdict("ok", None, None)

    faturado = Decimal(str(invoice_price))
    cotado = Decimal(str(quote_price))
    diferenca = faturado - cotado
    diferenca_pct = (diferenca / cotado * 100) if cotado else None

    minimo, maximo = cotado, cotado
    faixa = parse_price_range(price_raw)
    if faixa is not None:
        minimo, maximo = min(faixa[0], cotado), max(faixa[1], cotado)

    if minimo <= faturado <= maximo:
        return PriceVerdict("ok", diferenca, diferenca_pct)
    if within_tolerance(faturado, maximo, abs_tol, pct_tol) or \
            within_tolerance(faturado, minimo, abs_tol, pct_tol):
        return PriceVerdict("ok", diferenca, diferenca_pct)
    return PriceVerdict("above" if faturado > maximo else "below", diferenca, diferenca_pct)


def invoice_line_total(
    quantity: Decimal | float | None,
    unit_price: Decimal | float | None,
    total_price: Decimal | float | None,
) -> Decimal | None:
    """Valor faturado da linha: `valor_linha`; se ausente, cai pra qtd x preco
    unitario (mesmo fallback do fluxograma)."""
    if total_price is not None:
        return Decimal(str(total_price))
    if quantity is not None and unit_price is not None:
        return Decimal(str(quantity)) * Decimal(str(unit_price))
    return None


# ---------------------------------------------------------------------------
# Grades de carne (secao 6) e variante de estado da MESMA invoice (secao 7)
# ---------------------------------------------------------------------------

def grades_in(text: str | None) -> frozenset[str]:
    """Grades citadas no texto ('BEEF Coulotte 2pc (Select)' -> {'SELECT'})."""
    tokens = set(norm_text(text).split())
    return frozenset(grade for grade in _GRADES if grade in tokens)


def grades_conflict(a: str | None, b: str | None) -> bool:
    """True se os dois lados citam grades e elas nao tem nada em comum."""
    grades_a, grades_b = grades_in(a), grades_in(b)
    return bool(grades_a and grades_b and not (grades_a & grades_b))


def _sem_palavras_estado(texto_normalizado: str) -> str:
    """Tira FRESH/FROZEN e abreviacoes do texto ja normalizado."""
    return " ".join(
        token for token in texto_normalizado.split() if token not in _PALAVRAS_ESTADO
    )


def mesmo_item_estado_diferente(
    desc_a: str | None,
    desc_b: str | None,
    sinonimos: Mapping[str, str] | None = None,
    score_cutoff: float = MESMO_ITEM_ESTADO_DIFERENTE_SCORE,
) -> bool:
    """True se as duas descricoes sao o MESMO produto entregue em estado diferente
    (fresco x congelado) e podem virar um grupo so (secao 7)."""
    if grades_conflict(desc_a, desc_b):
        return False
    normalizado_a = norm_item(desc_a, sinonimos)
    normalizado_b = norm_item(desc_b, sinonimos)
    # O estado e o que tem de diferente: sem isso, duas linhas que nem falam de
    # estado se fundiriam so pelo subconjunto de tokens.
    estados_a = {t for t in normalizado_a.split() if t in _PALAVRAS_ESTADO}
    estados_b = {t for t in normalizado_b.split() if t in _PALAVRAS_ESTADO}
    if estados_a == estados_b:
        return False
    texto_a = _sem_palavras_estado(normalizado_a)
    texto_b = _sem_palavras_estado(normalizado_b)
    if not texto_a or not texto_b:
        return False
    return fuzz.token_set_ratio(texto_a, texto_b, score_cutoff=score_cutoff) >= score_cutoff


# ---------------------------------------------------------------------------
# Tipos de entrada e saida do match. Semantica dos campos do PO: secao 12.
# ---------------------------------------------------------------------------

class InvoiceLine(NamedTuple):
    key: Any                       # invoice_items.id
    description: str | None
    unit_price: Decimal | None


class QuoteLine(NamedTuple):
    key: Any                       # price_quote.id
    item_name: str | None
    price: Decimal
    price_raw: str | None = None


class ItemMatch(NamedTuple):
    invoice_key: Any
    quote_key: Any | None
    match_level: str               # 'exact' | 'fuzzy' | 'unmatched'
    match_score: float | None
    ambiguous: bool = False        # empate no desempate -> conferencia humana


class POLine(NamedTuple):
    """Linha de item do PO do Catapult, como raspada da aba Items
    (`commons/catapult`). Unidades e `unit`/`receipt_alias`: secao 12."""

    key: Any
    supplier_unit_id: str | None
    scancode: str | None
    item_name: str | None
    ordered: Decimal | None = None
    received: Decimal | None = None
    invoiced_total_cost: Decimal | None = None   # valor TOTAL da linha
    receipt_alias: str | None = None
    invoiced_unit_cost: Decimal | None = None    # custo por unidade (secao 13)
    ordered_unit_cost: Decimal | None = None     # custo por unidade, so carne (secao 13)


class InvoiceCodeLine(NamedTuple):
    """Linha da invoice, so o que o match contra o PO precisa pra achar o par.
    Quantidade e preco entram depois (`conciliacao/reconcile_erp.py`)."""

    key: Any
    description: str | None
    item_code: str | None
    upc: str | None
    handwritten_code: str | None = None   # mais um candidato de codigo (secao 11)
    valor: Decimal | None = None          # total da linha, p/ casar por valor (Etapa 2c)


class POMatch(NamedTuple):
    invoice_key: Any
    po_key: Any | None
    match_level: str               # 'codigo' | 'nome' | 'unmatched'
    match_score: float | None
    ambiguous: bool = False        # so 'nome' produz score < CONFIDENT_SCORE


# ---------------------------------------------------------------------------
# Nome de fornecedor (secao 8)
# ---------------------------------------------------------------------------

def match_supplier(
    name: str | None,
    candidates: Sequence[str],
    threshold: float = 80.0,
) -> tuple[str | None, float]:
    """Melhor candidato para um nome de fornecedor, por `token_set_ratio`.

    So pra SUGERIR de-para para revisao humana, nunca pra decidir sozinho (secao 8).
    """
    alvo = norm_supplier(name)
    if not alvo or not candidates:
        return None, 0.0
    melhor, melhor_score, melhor_comuns = None, 0.0, 0
    tokens_alvo = set(alvo.split())
    for candidato in candidates:
        candidato_norm = norm_supplier(candidato)
        if len(candidato_norm) < _MIN_CHARS_CANDIDATO_SUPPLIER and candidato_norm != alvo:
            continue
        score = fuzz.token_set_ratio(alvo, candidato_norm)
        comuns = len(tokens_alvo & set(candidato_norm.split()))
        # Empate de score: vence quem tem mais palavras em comum com o nome lido.
        if score > melhor_score or (score == melhor_score and comuns > melhor_comuns):
            melhor, melhor_score, melhor_comuns = candidato, score, comuns
    return (melhor, melhor_score) if melhor_score >= threshold else (None, melhor_score)


def pontuar_nome_catapult(nome: str | None, prefixo: str | None) -> float:
    """Similaridade 0-100 entre o nome buscado e um prefixo de Name de PO: 100 por
    subconjunto de tokens que identifique, senao o melhor fuzzy (secao 8)."""
    nome_norm, prefixo_norm = norm_supplier(nome), norm_supplier(prefixo)
    if not nome_norm or not prefixo_norm:
        return 0.0
    tokens_nome = set(nome_norm.lower().split())
    tokens_prefixo = set(prefixo_norm.lower().split())
    menor = tokens_nome if len(tokens_nome) <= len(tokens_prefixo) else tokens_prefixo
    subconjunto = tokens_nome <= tokens_prefixo or tokens_prefixo <= tokens_nome
    if subconjunto and menor - _TOKENS_GENERICOS:
        return 100.0
    return max(
        fuzz.token_sort_ratio(nome_norm, prefixo_norm),
        fuzz.ratio(nome_norm.replace(" ", ""), prefixo_norm.replace(" ", "")),
    )


def aceitar_prefixos_catapult(
    nomes: Sequence[str | None], prefixos: Sequence[str], piso: float = _PISO_NOME_CATAPULT,
) -> tuple[list[str], dict[str, float]]:
    """Prefixos de PO que sao confiavelmente o mesmo fornecedor de `nomes`.

    Devolve `(aceitos, scores)` - `scores` e a melhor pontuacao de cada prefixo
    distinto, pra diagnostico em log. Igualdade tem precedencia; mais de um
    aceito = ambiguo, quem chama desempata por itens e valor (secao 8).
    """
    distintos = list(dict.fromkeys(prefixo for prefixo in prefixos if prefixo))
    scores = {
        prefixo: max((pontuar_nome_catapult(nome, prefixo) for nome in nomes), default=0.0)
        for prefixo in distintos
    }
    alvos = {norm_supplier(nome) for nome in nomes if norm_supplier(nome)}
    exatos = [prefixo for prefixo in distintos if norm_supplier(prefixo) in alvos]
    if exatos:
        return exatos, scores
    return [prefixo for prefixo in distintos if scores[prefixo] >= piso], scores


# ---------------------------------------------------------------------------
# Atribuicao 1-para-1, usada pelas duas frentes (secao 5)
# ---------------------------------------------------------------------------

def _atribuir_guloso(
    pares: list[tuple[float, float, int, int]],
) -> dict[int, tuple[float, int, bool]]:
    """1-para-1 guloso sobre `(score, score_simetrico, i, j)`: o maior score
    ganha, o simetrico desempata e cada indice so e usado uma vez; o resto tenta
    o proximo candidato acima do piso (secao 5).

    Devolve `{i: (score, j, indistinguivel)}`. `indistinguivel` = havia outro
    candidato com os DOIS scores iguais disputando o mesmo `i` ou o mesmo `j`,
    ou seja, nem o simetrico separou os dois - quem chama marca `ambiguous`.
    """
    ordenados = sorted(pares, key=lambda par: (-par[0], -par[1], par[2], par[3]))
    empates_invoice = Counter((score, simetrico, i) for score, simetrico, i, _ in pares)
    empates_outro = Counter((score, simetrico, j) for score, simetrico, _, j in pares)

    usados_invoice: set[int] = set()
    usados_outro: set[int] = set()
    escolhidos: dict[int, tuple[float, int, bool]] = {}
    for score, simetrico, i, j in ordenados:
        if i in usados_invoice or j in usados_outro:
            continue
        usados_invoice.add(i)
        usados_outro.add(j)
        indistinguivel = (empates_invoice[(score, simetrico, i)] > 1
                          or empates_outro[(score, simetrico, j)] > 1)
        escolhidos[i] = (score, j, indistinguivel)
    return escolhidos


# ---------------------------------------------------------------------------
# Match de itens contra a cotacao (secao 10)
# ---------------------------------------------------------------------------

def match_items(
    invoice_lines: Sequence[InvoiceLine],
    quote_lines: Sequence[QuoteLine],
    fuzzy_threshold: float = FUZZY_THRESHOLD,
    abs_tol: Decimal = ABS_TOL,
    pct_tol: Decimal = PCT_TOL,
    *,
    sinonimos: Mapping[str, str] | None = None,
) -> list[ItemMatch]:
    """Casa itens da invoice com itens da cotacao, em cascata: preco, nome sobre o
    residuo, resto 'unmatched' (secao 10). A ordem de `invoice_lines` e preservada."""
    cotacoes = [
        cotacao for cotacao in quote_lines
        if cotacao.price is not None and Decimal(str(cotacao.price)) > 0
    ]

    matches: dict[Any, ItemMatch] = {}
    residuo: list[InvoiceLine] = []

    # --- Etapa 1: assinatura de preco -------------------------------------
    for linha in invoice_lines:
        if linha.unit_price is None:
            residuo.append(linha)
            continue

        candidatos = [
            cotacao for cotacao in cotacoes
            if within_tolerance(linha.unit_price, cotacao.price, abs_tol, pct_tol)
            and not grades_conflict(linha.description, cotacao.item_name)
        ]
        if not candidatos:
            residuo.append(linha)
            continue

        texto_invoice = norm_item(linha.description, sinonimos)
        pontuados = []
        for cotacao in candidatos:
            texto_cotacao = norm_item(cotacao.item_name, sinonimos)
            pontuados.append((
                float(fuzz.token_set_ratio(texto_invoice, texto_cotacao)),
                float(fuzz.token_sort_ratio(texto_invoice, texto_cotacao)),
                cotacao,
            ))
        pontuados.sort(key=lambda par: (-par[0], -par[1]))

        melhor_score, melhor_simetrico, melhor_cotacao = pontuados[0]
        if melhor_score < EXACT_MIN_SCORE:
            residuo.append(linha)
            continue

        empatado = sum(1 for score, simetrico, _ in pontuados
                       if (score, simetrico) == (melhor_score, melhor_simetrico)) > 1
        matches[linha.key] = ItemMatch(
            linha.key, melhor_cotacao.key, "exact", melhor_score,
            ambiguous=empatado or melhor_score < CONFIDENT_SCORE,
        )

    # --- Etapa 2: nome do item, sobre o residuo ---------------------------
    # Laco simples em vez de rapidfuzz.process.cdist: cdist exige numpy so pra
    # montar a matriz, e aqui ela e minuscula (um fornecedor por vez).
    if residuo and cotacoes:
        textos_invoice = [norm_item(linha.description, sinonimos) for linha in residuo]
        textos_cotacao = [norm_item(cotacao.item_name, sinonimos) for cotacao in cotacoes]

        pares: list[tuple[float, float, int, int]] = []
        for idx_invoice, texto_invoice in enumerate(textos_invoice):
            for idx_cotacao, texto_cotacao in enumerate(textos_cotacao):
                score = fuzz.token_set_ratio(
                    texto_invoice, texto_cotacao, score_cutoff=fuzzy_threshold
                )
                if score and not grades_conflict(residuo[idx_invoice].description,
                                                 cotacoes[idx_cotacao].item_name):
                    simetrico = float(fuzz.token_sort_ratio(texto_invoice, texto_cotacao))
                    pares.append((float(score), simetrico, idx_invoice, idx_cotacao))

        for idx_invoice, (score, idx_cotacao, indistinguivel) in _atribuir_guloso(pares).items():
            matches[residuo[idx_invoice].key] = ItemMatch(
                residuo[idx_invoice].key, cotacoes[idx_cotacao].key, "fuzzy", score,
                ambiguous=score < CONFIDENT_SCORE or indistinguivel,
            )

    # --- Etapa 3: o que sobrou --------------------------------------------
    return [
        matches.get(linha.key, ItemMatch(linha.key, None, "unmatched", None))
        for linha in invoice_lines
    ]


# ---------------------------------------------------------------------------
# Match de itens contra o PO do Catapult, comparacao='erp' (secoes 11 e 12)
# ---------------------------------------------------------------------------

# "xxx" no Supplier Unit ID e placeholder de codigo nao cadastrado no Catapult,
# nunca um codigo de catalogo de verdade (achado real: Smirnoff Ice Pack,
# Florida Distributing - spec-codigo-placeholder-casa-por-nome). So o unico
# valor confirmado; novo valor entra so com caso real.
_CODIGOS_PLACEHOLDER = {"xxx"}


def _codigo_e_placeholder(po: POLine) -> bool:
    return (po.supplier_unit_id or "").strip().lower() in _CODIGOS_PLACEHOLDER


def _po_zerado(po: POLine) -> bool:
    """True quando `received` E `invoiced_total_cost` sao CONHECIDOS e os dois
    zero: nada foi recebido nem faturado nessa linha (secoes 11 e 12)."""
    recebido, custo = po.received, po.invoiced_total_cost
    return recebido is not None and custo is not None and recebido == 0 and custo == 0


def _indice_por_codigo(po_lines: Sequence[POLine]) -> dict[str, POLine]:
    """Codigo normalizado -> primeira PO que o tem, testando `supplier_unit_id` e
    depois `scancode`, na ordem de `po_lines`."""
    indice: dict[str, POLine] = {}
    for po in po_lines:
        for codigo in (norm_code(po.supplier_unit_id), norm_code(po.scancode)):
            if codigo:
                indice.setdefault(codigo, po)
    return indice


def _po_por_codigo(
    line: InvoiceCodeLine, code_index: dict[str, POLine], reclamados: set[Any],
    *, permitir_zerado: bool,
) -> POLine | None:
    """PO cujo codigo bate com item_code/upc/handwritten_code, nessa ordem de
    prioridade. `permitir_zerado` separa a Etapa 1 da 2b (secao 11). Cada
    candidato tenta `norm_code` (com letra) primeiro e, so se esse nao bater
    com nada no indice, `norm_code_so_digitos` sobre o MESMO valor cru
    (spec-codigo-na-nao-casa R7-R9) — nunca o contrario, pra nao reabrir a
    colisao que `norm_code` corrige."""
    candidatos = (line.item_code, line.upc, line.handwritten_code)
    for bruto in candidatos:
        for codigo in (norm_code(bruto), norm_code_so_digitos(bruto)):
            if not codigo or codigo not in code_index:
                continue
            po = code_index[codigo]
            if po.key in reclamados:
                continue
            if not permitir_zerado and _po_zerado(po):
                continue
            return po
    return None


def _po_por_valor(
    valor: Decimal | None, livres: Sequence[POLine],
) -> tuple[POLine | None, bool]:
    """`(po, duvida)`: linha do PO cujo `invoiced_total_cost` iguala `valor` ao
    centavo; com varias, a de `valor / received` mais perto de `ordered_unit_cost`.
    `duvida=True` quando ha varias e o unitario nao resolve (spec-conciliacao-so-por-valor)."""
    if valor is None or valor <= 0:
        return None, False
    candidatas = [
        po for po in livres
        if po.invoiced_total_cost is not None
        and within_tolerance(valor, po.invoiced_total_cost, ABS_TOL, Decimal("0"))
    ]
    if len(candidatas) <= 1:
        return (candidatas[0] if candidatas else None), False
    proximas = sorted(
        (abs(valor / po.received - po.ordered_unit_cost), idx, po)
        for idx, po in enumerate(candidatas)
        if po.received and po.ordered_unit_cost is not None
        and within_tolerance(valor / po.received, po.ordered_unit_cost)
    )
    if not proximas or (len(proximas) > 1 and proximas[0][0] == proximas[1][0]):
        return None, True
    return proximas[0][2], False


def _pontuar_pares_nome(
    residual: Sequence[InvoiceCodeLine], disponiveis: Sequence[POLine],
    sinonimos: Mapping[str, str] | None, fuzzy_threshold: float,
) -> list[tuple[float, float, int, int]]:
    """Score e score simetrico de cada par residuo x PO disponivel, pelo melhor
    entre `item_name` e `receipt_alias` do PO (secoes 5 e 11). Devolve
    (score, score_simetrico, indice_invoice, indice_po).

    PO com codigo placeholder (`_codigo_e_placeholder`,
    spec-codigo-placeholder-casa-por-nome) usa `PLACEHOLDER_MIN_SCORE` em vez
    de `fuzzy_threshold` - sem nenhum codigo pra apoiar o par, o piso normal
    (calibrado pra quando o codigo poderia ter batido e nao bateu) fica alto
    demais pro caso onde nunca havia codigo nenhum pra comecar."""
    textos_invoice = [norm_item(linha.description, sinonimos) for linha in residual]
    pares: list[tuple[float, float, int, int]] = []
    for idx_invoice, texto_invoice in enumerate(textos_invoice):
        for idx_po, po in enumerate(disponiveis):
            piso = PLACEHOLDER_MIN_SCORE if _codigo_e_placeholder(po) else fuzzy_threshold
            melhor = (0.0, 0.0)
            for nome in (po.item_name, po.receipt_alias):
                if not nome:
                    continue
                texto_po = norm_item(nome, sinonimos)
                score = fuzz.token_set_ratio(texto_invoice, texto_po,
                                             score_cutoff=piso)
                if score:
                    simetrico = float(fuzz.token_sort_ratio(texto_invoice, texto_po))
                    melhor = max(melhor, (float(score), simetrico))
            if melhor[0]:
                pares.append((melhor[0], melhor[1], idx_invoice, idx_po))
    return pares


def match_items_po(
    invoice_lines: Sequence[InvoiceCodeLine],
    po_lines: Sequence[POLine],
    fuzzy_threshold: float = FUZZY_THRESHOLD,
    *,
    sinonimos: Mapping[str, str] | None = None,
) -> list[POMatch]:
    """Casa itens da invoice com itens do PO so pelo valor da linha
    (spec-conciliacao-so-por-valor); codigo e nome estao comentados abaixo.
    Item em duvida (varias linhas de mesmo valor, unitario nao resolve) volta
    'unmatched' com `ambiguous=True`. A ordem de `invoice_lines` e preservada.
    """
    code_index = _indice_por_codigo(po_lines)

    matches: dict[Any, POMatch] = {}
    reclamados: set[Any] = set()
    residuo: list[InvoiceCodeLine] = []

    # --- Etapa 1: codigo, em linha de PO ativa ----------------------------
    # Sem checar `reclamados`: duas linhas da invoice com o MESMO codigo casam as
    # duas com a mesma PO e se somam depois em `_comparar_grupo`.
    # Desligado (spec-conciliacao-so-por-valor): match por codigo e por nome, so o valor casa.
    # for linha in invoice_lines:
    #     po = _po_por_codigo(linha, code_index, set(), permitir_zerado=False)
    #     if po is None:
    #         residuo.append(linha)
    #         continue
    #     matches[linha.key] = POMatch(linha.key, po.key, "codigo", None)
    #     reclamados.add(po.key)
    residuo = list(invoice_lines)

    # --- Etapa 1b: valor da linha (spec-conciliacao-so-por-valor) ----------
    # Varias linhas com o mesmo valor e sem desempate: sem par, em duvida.
    duvida_valor: set[Any] = set()
    for linha in residuo:
        livres = [po for po in po_lines if po.key not in reclamados and not _po_zerado(po)]
        po, duvida = _po_por_valor(linha.valor, livres)
        if duvida:
            duvida_valor.add(linha.key)
        elif po is not None:
            matches[linha.key] = POMatch(linha.key, po.key, "valor", None)
            reclamados.add(po.key)
    residuo = [l for l in residuo if l.key not in matches and l.key not in duvida_valor]

    def casar_por_nome(zeradas: bool) -> None:
        sem_par = [l for l in residuo if l.key not in matches]
        disponiveis = [
            po for po in po_lines
            if po.key not in reclamados and _po_zerado(po) == zeradas
        ]
        if not sem_par or not disponiveis:
            return
        pares = _pontuar_pares_nome(sem_par, disponiveis, sinonimos, fuzzy_threshold)
        for idx_invoice, (score, idx_po, indistinguivel) in _atribuir_guloso(pares).items():
            matches[sem_par[idx_invoice].key] = POMatch(
                sem_par[idx_invoice].key, disponiveis[idx_po].key, "nome", score,
                ambiguous=score < CONFIDENT_SCORE or indistinguivel,
            )
            reclamados.add(disponiveis[idx_po].key)

    # --- Etapa 2: nome, sobre o residuo, contra PO livre e nao zerado -----
    # casar_por_nome(zeradas=False)

    # --- Etapa 2b: codigo contra linha ZERADA, ultimo recurso -------------
    # Aqui `reclamados` JA conta: uma PO zerada so e dada pra uma linha.
    # for linha in invoice_lines:
    #     if linha.key in matches:
    #         continue
    #     po = _po_por_codigo(linha, code_index, reclamados, permitir_zerado=True)
    #     if po is not None:
    #         matches[linha.key] = POMatch(linha.key, po.key, "codigo", None)
    #         reclamados.add(po.key)

    # --- Etapa 2c: nome contra linha ZERADA que sobrou, ultimo recurso ----
    # casar_por_nome(zeradas=True)

    # --- Etapa 3: o que sobrou --------------------------------------------
    return [
        matches.get(linha.key, POMatch(linha.key, None, "unmatched", None,
                                       ambiguous=linha.key in duvida_valor))
        for linha in invoice_lines
    ]


def po_items_sem_invoice(
    po_lines: Sequence[POLine], matches: Sequence[POMatch],
) -> list[Any]:
    """Chaves do PO que nenhuma linha da invoice reclamou - o "reverse check" do
    fluxograma (pedido/recebido mas nao faturado).

    Hoje isto so alimenta relatorio/log: `fat_conciliacao_item` exige
    `id_invoice_item` (NOT NULL), entao persistir a lista como linha propria ainda
    depende de estender o schema.
    """
    reclamados = {match.po_key for match in matches if match.po_key is not None}
    return [po.key for po in po_lines if po.key not in reclamados]
