# -*- coding: utf-8 -*-
__title__ = "Cotar Parede Completo"
__version__ = "1.3"
__doc__ = (
    "v1.3: parede-a-parede na ordem certa (total -> subgrupos -> proxima "
    "parede), do perimetro/exterior para o interior da planta; fallback "
    "geometrico p/ nunca esquecer uma parede na cota total; deteccao de "
    "perimetro mais robusta (nao depende so de WallType.Function)."
)

# ============================================================
# IMPORTS
# ============================================================
from Autodesk.Revit.DB import (
    Options, Solid, PlanarFace, ReferenceArray, Line, XYZ,
    DimensionType, BuiltInParameter, BuiltInCategory,
    FilteredElementCollector, Dimension, Wall, FamilyInstance,
    ElementCategoryFilter, LogicalOrFilter, WallFunction,
)

from pyrevit import revit, forms, script

doc    = revit.doc
uidoc  = revit.uidoc
output = script.get_output()
logger = script.get_logger()

# ------------------------------------------------------------
# Unidades (compat Revit 2021- / 2022+)
# ------------------------------------------------------------
try:
    from Autodesk.Revit.DB import UnitTypeId, UnitUtils
    def to_ft(cm):
        return UnitUtils.ConvertToInternalUnits(cm, UnitTypeId.Centimeters)
    def to_cm(ft):
        return UnitUtils.ConvertFromInternalUnits(ft, UnitTypeId.Centimeters)
except ImportError:
    from Autodesk.Revit.DB import DisplayUnitType, UnitUtils
    def to_ft(cm):
        return UnitUtils.ConvertToInternalUnits(cm, DisplayUnitType.DUT_CENTIMETERS)
    def to_cm(ft):
        return UnitUtils.ConvertFromInternalUnits(ft, DisplayUnitType.DUT_CENTIMETERS)


# ============================================================
# CONFIG
# ============================================================
class Config(object):
    NOME_TIPO_COTA_PADRAO = "Cota - 2 mm (cm) - 1 casa decimal vermelha"
    ESCALA_BASE = 50.0

    # Tolerancias base (cm), escaladas pela escala da vista em runtime
    TOL_DIM_ZERO_CM      = 1.0    # faces mais proximas = duplicata (mesmo ponto)
    CLUSTER_TOL_CM       = 50.0   # separa alinhamentos/paredes diferentes
    COLA_ELEMENTO_CM     = 15.0   # distancia da cota "colada" ate o elemento
    GAP_NIVEL_CM         = 10.0   # espacamento entre niveis de linha (0->1->2)
    GAP_GERAL_CM         = 60.0   # afastamento extra da cota GERAL (nivel 3)
    MARGEM_PONTA_CM      = 20.0   # quanto a linha de cota estica alem das pontas

    # [v1.2] Cruzamentos perpendiculares (sub-cotas dentro da corrente)
    TOL_CRUZAMENTO_EXTENSAO_CM = 5.0    # quanto o cruzamento pode "estourar" a
                                          # extensao da parede (cantos/juntas)
    LARGURA_CRUZAMENTO_PADRAO_CM = 30.0  # fallback se a Width da parede
                                          # perpendicular nao puder ser lida
    FATOR_LARGURA_CRUZAMENTO = 1.5       # margem de busca da face mais
                                          # proxima = largura da parede * isso

    # Filtro de ruido: ignora faces menores que isso (m2)
    AREA_MINIMA_FACE_M2 = 0.02

    # Categorias auto-coletadas quando nao ha selecao previa
    CATEGORIAS_AUTO = [
        BuiltInCategory.OST_Walls,
        BuiltInCategory.OST_Doors,
        BuiltInCategory.OST_Windows,
        BuiltInCategory.OST_Floors,
        BuiltInCategory.OST_Stairs,
    ]


def fator_escala(view):
    escala = float(getattr(view, "Scale", None) or Config.ESCALA_BASE)
    return escala / Config.ESCALA_BASE


class Tolerancias(object):
    def __init__(self, view):
        f = fator_escala(view)
        self.fator = f
        self.tol_dim_zero  = to_ft(Config.TOL_DIM_ZERO_CM)
        self.cluster_tol   = to_ft(Config.CLUSTER_TOL_CM * f)
        self.cola_elemento = to_ft(Config.COLA_ELEMENTO_CM * f)
        self.gap_nivel     = to_ft(Config.GAP_NIVEL_CM * f)
        self.gap_geral     = to_ft(Config.GAP_GERAL_CM * f)
        self.margem_ponta  = to_ft(Config.MARGEM_PONTA_CM * f)
        # cruzamento nao escala com a vista - e' geometria real do modelo
        self.tol_cruzamento_extensao = to_ft(Config.TOL_CRUZAMENTO_EXTENSAO_CM)
        self.largura_cruzamento_padrao = to_ft(Config.LARGURA_CRUZAMENTO_PADRAO_CM)


def dot(a, b):
    return a.X * b.X + a.Y * b.Y + a.Z * b.Z


def identificar_paredes_exteriores(elementos):
    """Retorna o set de ElementId.IntegerValue das paredes cuja Function
    (parametro nativo do Revit, o mesmo usado no filtro 'Exterior/Interior'
    do proprio software) e' Exterior. Usado pra decidir quais alinhamentos
    sao 'perimetro do predio' (cota deve ir pra fora da casa) versus
    alinhamentos internos (cota fica colada no proprio elemento, onde ja
    estava). Se a leitura da Function falhar por qualquer motivo, a
    parede e' tratada como interior (comportamento antigo, mais seguro)."""
    exteriores = set()
    for el in elementos:
        if not isinstance(el, Wall):
            continue
        try:
            wt = el.WallType
            if wt is not None and wt.Function == WallFunction.Exterior:
                exteriores.add(el.Id.IntegerValue)
        except Exception as e:
            logger.debug("Nao foi possivel ler Function da parede {}: {}".format(
                el.Id.IntegerValue, e))
    return exteriores


# ============================================================
# ETAPA 1 - Coleta de elementos (selecao ou automatica na view inteira)
# ============================================================
def coletar_elementos(view):
    sel_ids = list(uidoc.Selection.GetElementIds())

    if sel_ids:
        elementos = []
        for eid in sel_ids:
            el = doc.GetElement(eid)
            if el is not None and el.Category is not None:
                elementos.append(el)
        output.print_md(
            "## Cotar Parede Completo - **{} elemento(s) selecionado(s)**".format(len(elementos))
        )
        return elementos

    # Nada selecionado -> "vasculha tudo": coleta as categorias relevantes
    # visiveis na propria view ativa.
    output.print_md("## Cotar Parede Completo - nenhuma selecao, coletando a view inteira...")
    filtros = [ElementCategoryFilter(c) for c in Config.CATEGORIAS_AUTO]
    or_filter = filtros[0]
    for f in filtros[1:]:
        or_filter = LogicalOrFilter(or_filter, f)

    try:
        elementos = list(
            FilteredElementCollector(doc, view.Id).WherePasses(or_filter).WhereElementIsNotElementType().ToElements()
        )
    except Exception as e:
        logger.error("Falha ao coletar elementos da view automaticamente: {}".format(e))
        forms.alert("Falha ao coletar elementos da view:\n{}".format(e), exitscript=True)
        return []

    elementos = [el for el in elementos if el.Category is not None]

    if not elementos:
        forms.alert(
            "Nenhum elemento (parede/porta/janela/piso/escada) encontrado nesta view.\n"
            "Selecione manualmente os elementos e rode de novo.",
            exitscript=True,
        )

    por_cat = {}
    for el in elementos:
        nm = el.Category.Name
        por_cat[nm] = por_cat.get(nm, 0) + 1
    resumo = ", ".join("{}: {}".format(k, v) for k, v in sorted(por_cat.items()))
    output.print_md("**{} elemento(s) coletado(s)** ({})".format(len(elementos), resumo))
    return elementos


# ============================================================
# ETAPA 2 - Extracao de faces referenciaveis (com cache + host_id)
# ============================================================
_geom_cache = {}  # ElementId.IntegerValue -> lista de Solids ja extraidos


def _solidos_do_elemento(element, opt):
    key = element.Id.IntegerValue
    if key in _geom_cache:
        return _geom_cache[key]

    solidos = []
    try:
        geom = element.get_Geometry(opt)
    except Exception as e:
        logger.debug("Falha ao ler geometria de {}: {}".format(key, e))
        _geom_cache[key] = solidos
        return solidos

    if geom is None:
        _geom_cache[key] = solidos
        return solidos

    for g in geom:
        if isinstance(g, Solid) and g.Volume > 0:
            solidos.append(g)
        elif hasattr(g, "GetInstanceGeometry"):
            try:
                for g2 in g.GetInstanceGeometry():
                    if isinstance(g2, Solid) and g2.Volume > 0:
                        solidos.append(g2)
            except Exception as e:
                logger.debug("Falha ao ler GeometryInstance de {}: {}".format(key, e))

    _geom_cache[key] = solidos
    return solidos


def _area_minima_ft2():
    return Config.AREA_MINIMA_FACE_M2 * 10.7639


def _obter_host_id(el):
    """'Dono' geometrico do elemento, usado para a cota GERAL DE CADA
    PAREDE: Wall -> o proprio Id; porta/janela hospedada -> Id da
    parede-host; demais (piso, escada) -> None (entram so no alinhamento
    por posicao, sem virar 'parede individual')."""
    if isinstance(el, Wall):
        return el.Id.IntegerValue
    if isinstance(el, FamilyInstance):
        try:
            host = el.Host
            if host is not None:
                return host.Id.IntegerValue
        except Exception as e:
            logger.debug("Sem Host para {}: {}".format(el.Id.IntegerValue, e))
    return None


def extrair_faces_referenciaveis(elementos, axis_dir, perp_dir, threshold=0.8):
    """Retorna lista de dicts {pos_axis, pos_perp, ref, host_id} para cada
    face plana referenciavel alinhada ao eixo escolhido. Qualquer falha de
    geometria em UM elemento e reportada e pulada - nunca derruba a
    execucao inteira."""
    opt = Options()
    opt.ComputeReferences = True
    area_min = _area_minima_ft2()

    resultado = []
    for el in elementos:
        host_id = _obter_host_id(el)
        try:
            solidos = _solidos_do_elemento(el, opt)
        except Exception as e:
            output.print_md("  [AVISO] elemento {} ignorado (erro de geometria): {}".format(
                el.Id.IntegerValue, e))
            continue

        for solid in solidos:
            try:
                faces = solid.Faces
            except Exception as e:
                logger.debug("Solido sem faces legiveis: {}".format(e))
                continue
            for face in faces:
                try:
                    if face.Reference is None or not isinstance(face, PlanarFace):
                        continue
                    if face.Area < area_min:
                        continue
                    d = dot(face.FaceNormal, axis_dir)
                    if abs(d) <= threshold:
                        continue
                    pos_axis = dot(face.Origin, axis_dir)
                    pos_perp = dot(face.Origin, perp_dir)
                    resultado.append({
                        "pos_axis": pos_axis, "pos_perp": pos_perp,
                        "ref": face.Reference, "host_id": host_id,
                    })
                except Exception as e:
                    logger.debug("Face ignorada por erro: {}".format(e))
                    continue

    return resultado


# ============================================================
# ETAPA 3 - Deduplicacao por posicao + agrupamento por alinhamento
# ============================================================
def dedupe_por_posicao(itens, tol):
    """itens: lista de dicts ordenada por pos_axis. Remove faces
    praticamente coincidentes no eixo de cota (mesma posicao)."""
    if not itens:
        return []
    aceitos = [itens[0]]
    for it in itens[1:]:
        diff = abs(it["pos_axis"] - aceitos[-1]["pos_axis"])
        if diff > tol:
            aceitos.append(it)
        else:
            logger.debug("Face a {:.2f}cm da anterior - descartada (duplicata)".format(to_cm(diff)))
    return aceitos


def agrupar_por_alinhamento(itens, tol):
    """Agrupa faces por proximidade da coordenada PERPENDICULAR ao eixo -
    cada grupo = um alinhamento (uma ou mais paredes em fileira)."""
    if not itens:
        return []
    ordenado = sorted(itens, key=lambda t: t["pos_perp"])
    grupos = [[ordenado[0]]]
    for item in ordenado[1:]:
        if abs(item["pos_perp"] - grupos[-1][-1]["pos_perp"]) <= tol:
            grupos[-1].append(item)
        else:
            grupos.append([item])
    return grupos


def montar_correntes(itens, tol_cluster, tol_dedup, paredes_exteriores):
    """Cada corrente = {'itens': [...ordenados por pos_axis, dedup...],
    'perp': media, 'host_ids': set de paredes presentes,
    'perimetro': True se alguma parede da corrente for Exterior}."""
    grupos = agrupar_por_alinhamento(itens, tol_cluster)
    correntes = []
    for grupo in grupos:
        grupo_ordenado = sorted(grupo, key=lambda t: t["pos_axis"])
        grupo_dedup = dedupe_por_posicao(grupo_ordenado, tol_dedup)
        if len(grupo_dedup) < 2:
            continue
        perp_medio = sum(t["pos_perp"] for t in grupo_dedup) / len(grupo_dedup)
        host_ids = set(t["host_id"] for t in grupo_dedup if t["host_id"] is not None)
        perimetro = bool(host_ids & paredes_exteriores)
        correntes.append({
            "itens": grupo_dedup, "perp": perp_medio,
            "host_ids": host_ids, "perimetro": perimetro,
        })
    return correntes


def marcar_perimetro_por_extremos(correntes):
    """[melhoria] Fallback de deteccao de perimetro: alem da Function do
    WallType (nem sempre configurada corretamente pelo usuario), tambem
    trata como 'perimetro' a corrente mais externa em CADA lado do eixo
    (a de menor 'perp' e a de maior 'perp'). Isso garante que a fachada
    mais de fora sempre saia colada por fora da planta, mesmo quando o
    tipo de parede nao foi marcado como Exterior - e' exatamente esse o
    padrao visto nas cotas '650.0'/'511.0' que ja funcionam bem hoje."""
    if len(correntes) < 2:
        if correntes:
            correntes[0]["perimetro"] = True
        return correntes
    mais_externa_min = min(correntes, key=lambda c: c["perp"])
    mais_externa_max = max(correntes, key=lambda c: c["perp"])
    mais_externa_min["perimetro"] = True
    mais_externa_max["perimetro"] = True
    return correntes


# ============================================================
# ETAPA 3.5 [v1.2] - Sub-cotas por cruzamento perpendicular
# ============================================================
def _linha_da_parede(wall):
    """Retorna (p0, p1) da LocationCurve da parede, so se for reta (Line).
    Paredes curvas ou sem LocationCurve sao ignoradas (retorna None) -
    nao entram na logica de cruzamento, mas continuam cotadas normalmente
    pelo resto do pipeline."""
    try:
        loc = wall.Location
        curve = getattr(loc, "Curve", None)
        if curve is None or not isinstance(curve, Line):
            return None
        return curve.GetEndPoint(0), curve.GetEndPoint(1)
    except Exception as e:
        logger.debug("Sem LocationCurve reta para parede {}: {}".format(
            getattr(wall.Id, "IntegerValue", "?"), e))
        return None


def _intersecao_2d(p1, d1n, p2, d2n):
    """Interseccao de duas retas (no plano XY) definidas por ponto+direcao
    UNITARIA. Retorna (t, s) = distancia ao longo de d1n/d2n ate o ponto de
    encontro, ou None se forem paralelas."""
    denom = d1n.X * d2n.Y - d1n.Y * d2n.X
    if abs(denom) < 1e-9:
        return None
    dx = p2.X - p1.X
    dy = p2.Y - p1.Y
    t = (dx * d2n.Y - dy * d2n.X) / denom
    s = (dx * d1n.Y - dy * d1n.X) / denom
    return t, s


def _cruzamentos_perpendiculares(host_wall, candidatas, perp_dir, tol_extensao):
    """Acha, entre 'candidatas', as paredes cuja linha de eixo e'
    aproximadamente perpendicular ao eixo de cota (alinhada com perp_dir)
    E cruza a extensao da parede host_wall (com folga de tol_extensao pra
    pegar cantos/juntas no limite). Retorna lista de (wall, ponto_3d)."""
    resultado = []
    linha_host = _linha_da_parede(host_wall)
    if linha_host is None:
        return resultado
    p0, p1 = linha_host
    d1 = XYZ(p1.X - p0.X, p1.Y - p0.Y, 0.0)
    len1 = d1.GetLength()
    if len1 < 1e-6:
        return resultado
    d1n = d1.Normalize()

    for w in candidatas:
        if w.Id.IntegerValue == host_wall.Id.IntegerValue:
            continue
        linha_w = _linha_da_parede(w)
        if linha_w is None:
            continue
        q0, q1 = linha_w
        d2 = XYZ(q1.X - q0.X, q1.Y - q0.Y, 0.0)
        len2 = d2.GetLength()
        if len2 < 1e-6:
            continue
        d2n = d2.Normalize()

        # a parede candidata precisa ser ~perpendicular ao eixo de cota
        # (ou seja, alinhada com a direcao perpendicular ao eixo)
        if abs(dot(d2n, perp_dir)) < 0.8:
            continue

        inter = _intersecao_2d(p0, d1n, q0, d2n)
        if inter is None:
            continue
        t, s = inter
        if t < -tol_extensao or t > len1 + tol_extensao:
            continue
        if s < -tol_extensao or s > len2 + tol_extensao:
            continue

        ponto = XYZ(p0.X + d1n.X * t, p0.Y + d1n.Y * t, p0.Z)
        resultado.append((w, ponto))

    return resultado


def adicionar_cruzamentos_perpendiculares(correntes, itens_todos, elementos, axis, perp, tolz):
    """[v1.2] Para cada corrente, procura paredes perpendiculares que
    cruzam alguma das paredes-host dessa corrente NO MEIO dela (nao so nas
    pontas) e injeta um ponto de referencia extra ali - reaproveitando a
    face que 'extrair_faces_referenciaveis' ja extraiu para essa parede
    perpendicular nesse mesmo eixo (nunca cria Reference nova).

    Isso NUNCA corta a corrente: so adiciona pontos intermediarios, entao
    a cota total (parede_total/alinhamento_total) continua ponta-a-ponta
    igual antes; so o nivel 'vaos' ganha mais segmentos (sub-cotas)."""
    paredes_por_id = {}
    for el in elementos:
        if isinstance(el, Wall):
            paredes_por_id[el.Id.IntegerValue] = el
    todas_paredes = list(paredes_por_id.values())

    total_adicionados = 0
    for c in correntes:
        itens_c = c["itens"]
        host_ids = sorted(set(it["host_id"] for it in itens_c if it["host_id"] is not None))
        pontos_add = []

        for hid in host_ids:
            host_wall = paredes_por_id.get(hid)
            if host_wall is None:
                continue

            try:
                cruzamentos = _cruzamentos_perpendiculares(
                    host_wall, todas_paredes, perp, tolz.tol_cruzamento_extensao
                )
            except Exception as e:
                logger.debug("Falha ao buscar cruzamentos da parede {}: {}".format(hid, e))
                continue

            for w, ponto in cruzamentos:
                pos_axis_cruz = dot(ponto, axis)
                wid = w.Id.IntegerValue

                candidatos = [it for it in itens_todos if it["host_id"] == wid]
                if not candidatos:
                    continue

                melhor = min(candidatos, key=lambda it: abs(it["pos_axis"] - pos_axis_cruz))

                try:
                    largura = w.Width  # ja vem em pes (unidade interna)
                    limite = largura * Config.FATOR_LARGURA_CRUZAMENTO
                except Exception:
                    limite = tolz.largura_cruzamento_padrao

                if abs(melhor["pos_axis"] - pos_axis_cruz) > limite:
                    # face mais proxima esta longe demais do cruzamento
                    # real - provavelmente pegou a ponta errada da parede,
                    # entao descarta pra nao inventar uma sub-cota errada.
                    continue

                pontos_add.append(melhor)

        if pontos_add:
            todos = itens_c + pontos_add
            todos_ordenados = sorted(todos, key=lambda t: t["pos_axis"])
            novo = dedupe_por_posicao(todos_ordenados, tolz.tol_dim_zero)
            total_adicionados += max(0, len(novo) - len(itens_c))
            c["itens"] = novo

    if total_adicionados:
        output.print_md("  [INFO] {} ponto(s) de cruzamento perpendicular adicionado(s) (sub-cotas).".format(
            total_adicionados))

    return correntes


# ============================================================
# ETAPA 3.6 [melhoria] - Garantir parede_total mesmo com juncao "comendo"
# a face de topo (nunca esquecer uma parede)
# ============================================================
def _extremos_wall_no_eixo(wall, axis):
    """Extremos ANALITICOS da parede projetados no eixo de cota, direto
    da LocationCurve - funciona mesmo quando a face de topo real sumiu
    da geometria solida por causa de juncao (T/L) com parede vizinha."""
    linha = _linha_da_parede(wall)
    if linha is None:
        return None
    p0, p1 = linha
    a0, a1 = dot(p0, axis), dot(p1, axis)
    return (min(a0, a1), max(a0, a1))


def _ref_mais_proxima_do_alvo(pos_alvo, candidatos, limite):
    if not candidatos:
        return None
    melhor = min(candidatos, key=lambda it: abs(it["pos_axis"] - pos_alvo))
    if abs(melhor["pos_axis"] - pos_alvo) > limite:
        return None
    return melhor


def garantir_pontos_parede(wall, pontos_parede, itens_c, itens_todos, axis, tolz):
    """[melhoria] Garante os 2 pontos (inicio/fim) da parede para a tarefa
    'parede_total', mesmo quando a juncao com parede(s) vizinha(s) faz a
    face de topo sumir da geometria solida (comum em juncoes L/T do
    Revit - motivo mais provavel de 'esquecer' uma parede).

    - Se ja existem >=2 pontos com o host_id da propria parede, usa eles
      (comportamento original, sem alteracao de resultado).
    - Caso contrario, calcula os extremos analiticos pela LocationCurve e
      busca a Reference existente mais proxima de cada extremo (primeiro
      dentro da mesma corrente, depois em toda a vista), dentro de uma
      folga baseada na largura real da parede - NUNCA inventa geometria
      nova, so reaproveita uma Reference ja extraida em outro lugar.
    - Se mesmo assim nao achar 2 pontos, avisa no output (em vez de
      silenciosamente deixar a parede sem cota nenhuma) e retorna o que
      tiver (a tarefa e' descartada depois se continuar com <2 pontos)."""
    if len(pontos_parede) >= 2:
        return sorted(pontos_parede, key=lambda t: t["pos_axis"])

    extremos = _extremos_wall_no_eixo(wall, axis)
    if extremos is None:
        return pontos_parede  # parede curva/sem LocationCurve reta - sem fallback possivel

    pos_min, pos_max = extremos
    try:
        limite = wall.Width * Config.FATOR_LARGURA_CRUZAMENTO
    except Exception:
        limite = tolz.largura_cruzamento_padrao

    achados = list(pontos_parede)

    for alvo in (pos_min, pos_max):
        if any(abs(it["pos_axis"] - alvo) <= limite for it in achados):
            continue
        candidato = _ref_mais_proxima_do_alvo(alvo, itens_c, limite)
        if candidato is None:
            candidato = _ref_mais_proxima_do_alvo(alvo, itens_todos, limite)
        if candidato is not None and candidato not in achados:
            achados.append(candidato)

    achados = sorted(achados, key=lambda t: t["pos_axis"])
    if len(achados) < 2:
        logger.debug("Parede {} sem 2 referencias validas para parede_total (fallback tambem falhou).".format(
            wall.Id.IntegerValue))
        output.print_md(
            "  [AVISO] parede {} nao pode ser cotada por completo (face de topo ausente na juncao "
            "e nenhuma referencia proxima encontrada para o fallback).".format(wall.Id.IntegerValue)
        )
        return achados
    return [achados[0], achados[-1]]


# ============================================================
# ETAPA 4 - Deduplicacao GLOBAL por referencia estavel (persistente)
# ============================================================
def stable_key(ref):
    """Chave estavel de uma Reference - usada pra saber se duas cotas
    (desta execucao ou de uma anterior, ja no modelo) apontam para
    exatamente a mesma geometria."""
    if ref is None:
        return None
    try:
        return ref.ConvertToStableRepresentation(doc)
    except Exception as e:
        logger.debug("Falha ConvertToStableRepresentation: {}".format(e))
        return None


def assinatura_da_tarefa(itens):
    chaves = []
    for it in itens:
        k = stable_key(it["ref"])
        if k is None:
            return None
        chaves.append(k)
    return frozenset(chaves)


def coletar_assinaturas_existentes(view):
    """Le as Dimension JA existentes na vista (de execucoes anteriores,
    inclusive manuais) e monta o conjunto de assinaturas, pra nunca
    recriar uma cota que ja existe."""
    existentes = set()
    try:
        dims = FilteredElementCollector(doc, view.Id).OfClass(Dimension).ToElements()
    except Exception as e:
        logger.error("Falha ao ler Dimension existentes na view: {}".format(e))
        return existentes

    for d in dims:
        try:
            refs = d.References
        except Exception:
            continue
        chaves = []
        ok = True
        for i in range(refs.Size):
            k = stable_key(refs.get_Item(i))
            if k is None:
                ok = False
                break
            chaves.append(k)
        if ok and chaves:
            existentes.add(frozenset(chaves))
    return existentes


# ============================================================
# ETAPA 5 - Geracao das tarefas de cota (vaos / parede / alinhamento / geral)
# ============================================================
def _ordenar_correntes_exterior_interior(correntes):
    """[melhoria] Ordem de processamento das correntes: TODAS as de
    perimetro (fora da casa) primeiro, depois as internas - dentro de
    cada grupo, ordenadas por 'perp' pra ficar deterministico. Isso
    implementa a rotina pedida: cota tudo que e' fachada/parede exterior
    primeiro (parede > total > subgrupos > proxima parede), e SO DEPOIS
    entra nas cotas internas da planta baixa."""
    return sorted(correntes, key=lambda c: (0 if c["perimetro"] else 1, c["perp"]))


def _atribuir_vaos_as_paredes(segmentos_vaos, ordem_paredes, extremos_por_wid, tol_extensao):
    """[melhoria] Associa cada segmento 'vao' (sub-cota) a UMA parede da
    corrente - a cujo intervalo [pos_min, pos_max] o meio do segmento
    pertence (com folga tol_extensao para pegar cantos/juntas no limite).
    Se nao pertencer com folga a nenhuma parede (ex.: piso sem parede
    identificavel), o segmento vai pra lista 'sem_dono', emitida no final
    da corrente. Isso e' o que permite a ordem 'parede > total >
    subgrupos DAQUELA parede > proxima parede'."""
    por_wid = {}
    sem_dono = []
    for a, b in segmentos_vaos:
        meio = (a["pos_axis"] + b["pos_axis"]) / 2.0
        dono = None
        for wid in ordem_paredes:
            pos_min, pos_max = extremos_por_wid[wid]
            if pos_min - tol_extensao <= meio <= pos_max + tol_extensao:
                dono = wid
                break
        if dono is None:
            sem_dono.append((a, b))
        else:
            por_wid.setdefault(dono, []).append((a, b))
    return por_wid, sem_dono


def gerar_tarefas_de_cota(correntes, itens_todos, tolz, paredes_por_id):
    """Monta as tarefas na ORDEM da rotina de cotagem manual:

      1) Correntes de PERIMETRO primeiro, correntes INTERNAS por ultimo
         (ver '_ordenar_correntes_exterior_interior');
      2) Dentro de cada corrente, PAREDE A PAREDE (esquerda->direita no
         eixo): para cada parede -> a) 'parede_total' (cota inteira,
         ponta a ponta, com fallback de geometria pra nunca 'esquecer'
         a parede - ver 'garantir_pontos_parede'); b) os 'vaos'
         (subgrupos/sub-cotas) que pertencem aquela mesma parede; so
         entao avanca pra proxima parede da corrente;
      3) 'alinhamento_total' (fileira inteira) so quando a corrente tem
         mais de 1 parede - fecha a corrente;
      4) por fim, 1x por eixo, a cota 'geral' - sempre por fora de tudo.

    Niveis (usados depois em resolver_layout para empilhar as linhas sem
    sobrepor, sempre se afastando mais da parede/fachada):
      0 = vaos (colado) | 1 = parede_total | 2 = alinhamento_total | 3 = geral
    """
    tarefas = []
    for c in _ordenar_correntes_exterior_interior(correntes):
        itens_c = c["itens"]
        tem_detalhe = len(itens_c) > 2
        perimetro = c["perimetro"]

        paredes_na_corrente = sorted(set(
            it["host_id"] for it in itens_c if it["host_id"] is not None
        ))

        # extremos analiticos (fallback) de cada parede da corrente,
        # usados tanto pra garantir a parede_total quanto pra decidir
        # quais vaos pertencem a qual parede.
        extremos_por_wid = {}
        for wid in paredes_na_corrente:
            wall = paredes_por_id.get(wid)
            extremos = _extremos_wall_no_eixo(wall, c.get("_axis")) if wall is not None else None
            if extremos is None:
                pontos_wid = [it for it in itens_c if it["host_id"] == wid]
                if pontos_wid:
                    extremos = (min(p["pos_axis"] for p in pontos_wid),
                                max(p["pos_axis"] for p in pontos_wid))
                else:
                    extremos = (0.0, 0.0)
            extremos_por_wid[wid] = extremos

        ordem_paredes = sorted(paredes_na_corrente, key=lambda wid: extremos_por_wid[wid][0])

        segmentos_vaos = [(itens_c[i], itens_c[i + 1]) for i in range(len(itens_c) - 1)] if tem_detalhe else []
        vaos_por_wid, vaos_sem_dono = _atribuir_vaos_as_paredes(
            segmentos_vaos, ordem_paredes, extremos_por_wid, tolz.tol_cruzamento_extensao
        ) if ordem_paredes else ({}, segmentos_vaos)

        # --- parede a parede: total -> subgrupos -> proxima parede ---
        for wid in ordem_paredes:
            wall = paredes_por_id.get(wid)
            pontos_parede = [it for it in itens_c if it["host_id"] == wid]
            if wall is not None:
                pontos_parede = garantir_pontos_parede(
                    wall, pontos_parede, itens_c, itens_todos, c.get("_axis"), tolz
                )
            if len(pontos_parede) >= 2:
                pontos_parede = sorted(pontos_parede, key=lambda t: t["pos_axis"])
                tarefas.append({
                    "nome": "parede_total",
                    "itens": [pontos_parede[0], pontos_parede[-1]],
                    "perp_ref": c["perp"], "nivel": 1 if tem_detalhe else 0,
                    "perimetro": perimetro,
                })
            else:
                logger.debug("Parede {} descartada de parede_total - menos de 2 pontos.".format(wid))

            for a, b in sorted(vaos_por_wid.get(wid, []), key=lambda ab: ab[0]["pos_axis"]):
                tarefas.append({
                    "nome": "vaos", "itens": [a, b],
                    "perp_ref": c["perp"], "nivel": 0, "perimetro": perimetro,
                })

        # vaos que nao puderam ser atribuidos a nenhuma parede (ex.: so
        # piso/escada) - emitidos no final da corrente, mesmo nivel 0.
        for a, b in sorted(vaos_sem_dono, key=lambda ab: ab[0]["pos_axis"]):
            tarefas.append({
                "nome": "vaos", "itens": [a, b],
                "perp_ref": c["perp"], "nivel": 0, "perimetro": perimetro,
            })

        # alinhamento inteiro (fileira) - fecha a corrente
        if len(ordem_paredes) > 1:
            tarefas.append({
                "nome": "alinhamento_total",
                "itens": [itens_c[0], itens_c[-1]],
                "perp_ref": c["perp"], "nivel": 2, "perimetro": perimetro,
            })
        elif not ordem_paredes:
            # nenhuma face pertence a uma parede identificavel (ex.: so
            # piso/escada relacionados) - ainda assim cota o alinhamento
            # inteiro, no nivel imediatamente acima do detalhe.
            tarefas.append({
                "nome": "alinhamento_total",
                "itens": [itens_c[0], itens_c[-1]],
                "perp_ref": c["perp"], "nivel": 1 if tem_detalhe else 0,
                "perimetro": perimetro,
            })

    if itens_todos:
        global_ordenado = sorted(itens_todos, key=lambda t: t["pos_axis"])
        global_dedup = dedupe_por_posicao(global_ordenado, tolz.tol_dim_zero)
        if len(global_dedup) >= 2:
            tarefas.append({
                "nome": "geral", "itens": [global_dedup[0], global_dedup[-1]],
                "perp_ref": None, "nivel": None,
            })

    return tarefas


def remover_tarefas_duplicadas(tarefas, assinaturas_existentes):
    """Remove tarefas cuja assinatura de referencias ja apareceu - seja em
    Dimension ja existente na view (execucao anterior/manual), seja em
    outra tarefa desta mesma execucao (ex.: parede sozinha no alinhamento
    -> parede_total == alinhamento_total; ou os dois eixos H/V gerando a
    mesma cota por coincidencia)."""
    vistos = set(assinaturas_existentes)
    resultado = []
    puladas = 0
    for t in tarefas:
        assinatura = assinatura_da_tarefa(t["itens"])
        if assinatura is None:
            # sem referencia estavel valida - deixa passar (sera pego no
            # try/except da criacao, que ja reporta erro por referencia
            # ausente/invalida) em vez de descartar silenciosamente.
            resultado.append(t)
            continue
        if assinatura in vistos:
            puladas += 1
            continue
        vistos.add(assinatura)
        resultado.append(t)
    if puladas:
        output.print_md("  [INFO] {} tarefa(s) descartada(s) por ja existirem (duplicata real).".format(puladas))
    return resultado


# ============================================================
# ETAPA 6 - Layout (posicao perpendicular final de cada tarefa)
# ============================================================
def resolver_layout(tarefas, centro_perp_modelo, tolz):
    """Decide o LADO/posicao final de cada alinhamento, sem precisar
    clicar um ponto:

    - Alinhamentos de PERIMETRO (tocam parede com Function=Exterior):
      ancorados no extremo REAL do predio naquele eixo (o menor/maior
      pos_perp entre TODAS as faces de paredes exteriores) - a cota fica
      colada por fora da casa, perto da parede, nao 'flutuando' pra
      dentro so porque um elemento interno prox puxou a media pro lado
      errado. Empilha nivel 0 (vaos/pedacos) -> 1 (parede) -> 2
      (alinhamento) nessa ordem, sempre se afastando mais da casa.

    - Alinhamentos internos (sem parede exterior): mantem o criterio
      antigo (lado que fica mais longe do centro do modelo), colado no
      proprio elemento - e' o esperado, ja que os pontos referenciados
      estao mesmo dentro da planta.
    """
    perp_por_alinhamento = {}
    for t in tarefas:
        if t["nome"] == "geral":
            continue
        perp_por_alinhamento.setdefault(t["perp_ref"], []).append(t)

    # Extremos reais do PERIMETRO (so entre alinhamentos marcados como
    # perimetro=True) - referencia para "fora da casa".
    perps_perimetro = [
        perp_ref for perp_ref, lista in perp_por_alinhamento.items()
        if lista and lista[0]["perimetro"]
    ]
    perimetro_min = min(perps_perimetro) if perps_perimetro else None
    perimetro_max = max(perps_perimetro) if perps_perimetro else None

    perp_extremos = []
    for perp_ref, lista in perp_por_alinhamento.items():
        eh_perimetro = lista[0]["perimetro"]

        if eh_perimetro and perimetro_min is not None and perimetro_max is not None:
            dist_min = abs(perp_ref - perimetro_min)
            dist_max = abs(perimetro_max - perp_ref)
            if dist_min <= dist_max:
                sinal = -1.0
                extremo = perimetro_min
            else:
                sinal = 1.0
                extremo = perimetro_max
            for t in lista:
                perp_pos = extremo + sinal * (tolz.cola_elemento + t["nivel"] * tolz.gap_nivel)
                t["perp_pos"] = perp_pos
                perp_extremos.append((sinal, perp_pos))
        else:
            # comportamento antigo: lado mais longe do centro do modelo,
            # colado na propria posicao do alinhamento.
            sinal = 1.0 if perp_ref >= centro_perp_modelo else -1.0
            for t in lista:
                base = perp_ref + sinal * tolz.cola_elemento
                perp_pos = base + sinal * (t["nivel"] * tolz.gap_nivel)
                t["perp_pos"] = perp_pos
                perp_extremos.append((sinal, perp_pos))

    for t in tarefas:
        if t["nome"] != "geral":
            continue
        if not perp_extremos:
            t["perp_pos"] = tolz.gap_geral
            continue
        # geral vai por fora de tudo, no lado que tiver mais elementos
        pos_sinal = [p for s, p in perp_extremos if s > 0]
        neg_sinal = [p for s, p in perp_extremos if s < 0]
        if len(pos_sinal) >= len(neg_sinal):
            t["perp_pos"] = (max(pos_sinal) if pos_sinal else 0.0) + tolz.gap_geral
        else:
            t["perp_pos"] = (min(neg_sinal) if neg_sinal else 0.0) - tolz.gap_geral

    return tarefas


# ============================================================
# ETAPA 7 - Criacao das cotas no Revit
# ============================================================
def find_dim_type_by_name(nome):
    try:
        dtypes = list(FilteredElementCollector(doc).OfClass(DimensionType).ToElements())
    except Exception as e:
        logger.error("Falha ao coletar DimensionType: {}".format(e))
        return None
    for dt in dtypes:
        try:
            nm = dt.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM).AsString()
        except Exception:
            nm = None
        if nm == nome:
            return dt
    if not dtypes:
        logger.debug("Nenhum DimensionType encontrado no documento.")
        return None
    output.print_md(
        "  [AVISO] tipo de cota '{}' nao encontrado - usando o primeiro disponivel.".format(nome)
    )
    return dtypes[0]


def _mpt(axis, perp, r, perp_pos):
    return XYZ(
        axis.X * r + perp.X * perp_pos,
        axis.Y * r + perp.Y * perp_pos,
        axis.Z * r + perp.Z * perp_pos,
    )


def _cria_dim_line(axis, perp, itens, perp_pos, margem):
    vals = [it["pos_axis"] for it in itens]
    r_min, r_max = min(vals) - margem, max(vals) + margem
    pt1, pt2 = _mpt(axis, perp, r_min, perp_pos), _mpt(axis, perp, r_max, perp_pos)
    if pt1.DistanceTo(pt2) < 1e-6:
        return None
    return Line.CreateBound(pt1, pt2)


def criar_cotas_no_revit(tarefas, view, axis, perp, tolz, dim_type):
    criadas, erros = 0, 0
    por_nome = {}
    with revit.Transaction("Cotar Parede Completo"):
        for t in tarefas:
            try:
                dim_line = _cria_dim_line(axis, perp, t["itens"], t["perp_pos"], tolz.margem_ponta)
                if dim_line is None:
                    logger.debug("Linha degenerada para tarefa '{}' - pulada.".format(t["nome"]))
                    continue

                ra = ReferenceArray()
                valido = True
                for it in t["itens"]:
                    if it["ref"] is None:
                        valido = False
                        break
                    ra.Append(it["ref"])
                if not valido:
                    output.print_md("[ERRO] tarefa '{}': referencia ausente, pulando.".format(t["nome"]))
                    erros += 1
                    continue

                nd = doc.Create.NewDimension(view, dim_line, ra)
                if dim_type:
                    try:
                        nd.DimensionType = dim_type
                    except Exception as e:
                        logger.debug("Falha ao aplicar DimensionType: {}".format(e))
                criadas += 1
                por_nome[t["nome"]] = por_nome.get(t["nome"], 0) + 1
            except Exception as e:
                erros += 1
                output.print_md("[ERRO] falha ao criar cota '{}': {}".format(t.get("nome", "?"), e))
    return criadas, erros, por_nome


# ============================================================
# MAIN
# ============================================================
def processar_eixo(elementos, view, axis, perp, nome_eixo, tolz, assinaturas_existentes, paredes_exteriores):
    """Roda o pipeline completo (extracao -> alinhamento -> cruzamentos ->
    tarefas -> dedup -> layout) para UM eixo (H ou V). Retorna a lista de
    tarefas prontas (com perp_pos definido) - a criacao no Revit e feita
    depois, juntando H + V numa unica transacao."""
    output.print_md("### Eixo {}".format(nome_eixo))

    itens = extrair_faces_referenciaveis(elementos, axis, perp)
    if len(itens) < 2:
        output.print_md("  [INFO] menos de 2 referencias nesse eixo - nada a cotar aqui.")
        return []

    correntes = montar_correntes(itens, tolz.cluster_tol, tolz.tol_dim_zero, paredes_exteriores)
    if not correntes:
        output.print_md("  [INFO] nenhum alinhamento valido nesse eixo.")
        return []

    # [melhoria] guarda o axis na propria corrente (usado depois pelo
    # fallback de garantir_pontos_parede) e amplia a deteccao de
    # perimetro para tambem cobrir a(s) corrente(s) mais externa(s) do
    # eixo, mesmo se o WallType nao estiver marcado como Exterior.
    for c in correntes:
        c["_axis"] = axis
    correntes = marcar_perimetro_por_extremos(correntes)

    n_perimetro = sum(1 for c in correntes if c["perimetro"])
    output.print_md("  {} alinhamento(s) encontrado(s) nesse eixo ({} de perimetro/exterior).".format(
        len(correntes), n_perimetro))

    # [v1.2] injeta pontos de cruzamento perpendicular (sub-cotas), sem
    # cortar a corrente - so acrescenta referencias no meio dela.
    correntes = adicionar_cruzamentos_perpendiculares(correntes, itens, elementos, axis, perp, tolz)

    paredes_por_id = {el.Id.IntegerValue: el for el in elementos if isinstance(el, Wall)}
    tarefas = gerar_tarefas_de_cota(correntes, itens, tolz, paredes_por_id)
    tarefas = remover_tarefas_duplicadas(tarefas, assinaturas_existentes)

    centro_perp_modelo = sum(t["pos_perp"] for t in itens) / len(itens)
    tarefas = resolver_layout(tarefas, centro_perp_modelo, tolz)

    for t in tarefas:
        t["_axis"] = axis
        t["_perp"] = perp
    return tarefas


def main():
    view = doc.ActiveView

    try:
        eixo_h = (view.RightDirection, view.UpDirection)
    except Exception as e:
        logger.error("Vista sem RightDirection/UpDirection utilizavel: {}".format(e))
        forms.alert(
            "Essa vista nao tem eixos H/V utilizaveis para cota (provavelmente "
            "nao e uma planta/elevacao/corte). Abra a view certa e rode de novo.",
            exitscript=True,
        )
        return

    tolz = Tolerancias(view)
    output.print_md(
        "Vista ativa: **{}** | Fator de escala: **{:.2f}x** (escala 1:{:.0f})".format(
            view.Name, tolz.fator, float(getattr(view, "Scale", None) or Config.ESCALA_BASE))
    )

    elementos = coletar_elementos(view)

    paredes_exteriores = identificar_paredes_exteriores(elementos)
    output.print_md("**{} parede(s) identificada(s) como Exterior** (Function do WallType) - "
        "as cotas de parede/pedaco dessas vao pra fora da casa.".format(len(paredes_exteriores)))

    assinaturas_existentes = coletar_assinaturas_existentes(view)
    output.print_md("**{} assinatura(s) de cota ja existente(s)** na vista (nao serao repetidas).".format(
        len(assinaturas_existentes)))

    dim_type = find_dim_type_by_name(Config.NOME_TIPO_COTA_PADRAO)

    axis_h, perp_h = eixo_h
    axis_v, perp_v = perp_h, axis_h  # eixo V e' simplesmente o H trocado

    tarefas_h = processar_eixo(elementos, view, axis_h, perp_h, "Horizontal", tolz, assinaturas_existentes, paredes_exteriores)
    tarefas_v = processar_eixo(elementos, view, axis_v, perp_v, "Vertical", tolz, assinaturas_existentes, paredes_exteriores)

    todas_tarefas = tarefas_h + tarefas_v
    if not todas_tarefas:
        forms.alert(
            "Nao foi possivel montar nenhuma tarefa de cota valida (H ou V).\n"
            "Verifique se os elementos coletados tem geometria solida normal.",
            exitscript=True,
        )
        return

    try:
        criadas_total, erros_total, por_nome_total = 0, 0, {}

        # Cria por eixo (cada chamada abre/fecha sua propria transacao
        # curta - assim um erro de commit num eixo nao contamina o outro).
        for nome_eixo, tarefas_eixo in (("Horizontal", tarefas_h), ("Vertical", tarefas_v)):
            if not tarefas_eixo:
                continue
            axis_ref = tarefas_eixo[0]["_axis"]
            perp_ref = tarefas_eixo[0]["_perp"]
            criadas, erros, por_nome = criar_cotas_no_revit(
                tarefas_eixo, view, axis_ref, perp_ref, tolz, dim_type
            )
            criadas_total += criadas
            erros_total += erros
            for k, v in por_nome.items():
                por_nome_total[k] = por_nome_total.get(k, 0) + v
    except Exception as e:
        logger.error("Falha critica ao criar cotas: {}".format(e))
        forms.alert("Falha critica ao criar cotas:\n{}".format(e), exitscript=True)
        return

    output.print_md("---")
    output.print_md(
        "## {} cota(s) criada(s): {} vao(s)/detalhe + {} parede(s) individual(is) + "
        "{} alinhamento(s) + {} geral(is).".format(
            criadas_total,
            por_nome_total.get("vaos", 0),
            por_nome_total.get("parede_total", 0),
            por_nome_total.get("alinhamento_total", 0),
            por_nome_total.get("geral", 0),
        )
    )
    if erros_total:
        output.print_md("**{} tarefa(s) falharam ao criar cota** - ver [ERRO] acima.".format(erros_total))


if __name__ == "__main__":
    main()
