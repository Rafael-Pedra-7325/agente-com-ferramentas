"""
agente-com-ferramentas | Loop de Function Calling com Guardrails e Observabilidade
Implementação de referência em Python usando o SDK oficial `google-genai`.

Recursos:
- Loop explícito de raciocínio e chamada de ferramentas com limite rígido de passos (`max_steps`)
- SQL somente leitura com dupla camada de proteção (validador léxico + `sqlite3.set_authorizer`)
- Busca documental ranqueada sobre base de conhecimento corporativa
- Calculadora segura baseada em AST (`ast.parse`, sem `eval()`)
- Trace estruturado passo a passo com latência (ms), payloads e exportação JSON
"""

from __future__ import annotations

import ast
import json
import math
import operator
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

try:
    from google import genai
    from google.genai import types
except ImportError:  # Permite executar testes unitários das ferramentas sem o SDK instalado
    genai = None  # type: ignore
    types = None  # type: ignore


# ==============================================================================
# 1. ESTRUTURAS DE OBSERVABILIDADE (TRACE)
# ==============================================================================


@dataclass
class TraceStep:
    step: int
    timestamp: str
    thought: str
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_output: dict[str, Any] | None = None
    status: str = "ok"  # "ok" | "blocked" | "error" | "final"
    latency_ms: float = 0.0


@dataclass
class AgentTrace:
    trace_id: str
    prompt: str
    model: str
    max_steps: int
    started_at: str
    finished_at: str = ""
    total_latency_ms: float = 0.0
    status: str = "running"  # "completed" | "max_steps_exceeded" | "error"
    final_answer: str = ""
    steps: list[TraceStep] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ==============================================================================
# 2. FERRAMENTAS COM GUARDRAILS
# ==============================================================================


class ReadOnlySQLEngine:
    """Motor SQLite em memória com bloqueio duplo contra escrita/DDL."""

    FORBIDDEN_KEYWORDS = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM|GRANT|REVOKE|EXEC)\b",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._seed_database()
        # Camada 2: Authorizer nativo do SQLite bloqueando qualquer operação que não seja SELECT/READ
        self.conn.set_authorizer(self._sqlite_authorizer)

    @staticmethod
    def _sqlite_authorizer(
        action_code: int,
        arg1: str | None,
        arg2: str | None,
        db_name: str | None,
        trigger_name: str | None,
    ) -> int:
        allowed_actions = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
        }
        return sqlite3.SQLITE_OK if action_code in allowed_actions else sqlite3.SQLITE_DENY

    def _seed_database(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE vendas (
                id INTEGER PRIMARY KEY,
                data TEXT NOT NULL,
                regiao TEXT NOT NULL,
                produto TEXT NOT NULL,
                categoria TEXT NOT NULL,
                receita REAL NOT NULL,
                custo REAL NOT NULL,
                unidades INTEGER NOT NULL
            );

            INSERT INTO vendas (id, data, regiao, produto, categoria, receita, custo, unidades) VALUES
                (1, '2026-10-03', 'Sudeste', 'Enterprise AI Suite', 'SaaS', 185000.00, 42000.00, 5),
                (2, '2026-10-12', 'Sul', 'DataGuard SQL Proxy', 'Seguranca', 92000.00, 21000.00, 4),
                (3, '2026-10-19', 'Sudeste', 'Observability Cloud', 'DevOps', 134000.00, 35000.00, 6),
                (4, '2026-11-02', 'Nordeste', 'Enterprise AI Suite', 'SaaS', 148000.00, 33600.00, 4),
                (5, '2026-11-08', 'Centro-Oeste', 'DataGuard SQL Proxy', 'Seguranca', 69000.00, 15750.00, 3),
                (6, '2026-11-14', 'Sul', 'Observability Cloud', 'DevOps', 112000.00, 29000.00, 5),
                (7, '2026-11-16', 'Sudeste', 'Enterprise AI Suite', 'SaaS', 222000.00, 50400.00, 6);

            CREATE TABLE chamados_sla (
                id INTEGER PRIMARY KEY,
                cliente TEXT NOT NULL,
                severidade TEXT NOT NULL,
                tempo_resposta_min INTEGER NOT NULL,
                sla_limite_min INTEGER NOT NULL,
                violado INTEGER NOT NULL
            );

            INSERT INTO chamados_sla VALUES
                (101, 'Banco Horizonte', 'P1', 12, 15, 0),
                (102, 'Logistica Sul', 'P1', 28, 15, 1),
                (103, 'Varejo Prime', 'P2', 45, 60, 0),
                (104, 'Fintech Nova', 'P1', 19, 15, 1);
            """
        )
        self.conn.commit()

    def execute(self, query: str) -> dict[str, Any]:
        cleaned = query.strip()
        if not cleaned:
            return {"ok": False, "error": "ERR_EMPTY_QUERY", "message": "A consulta SQL está vazia."}

        # Bloqueia múltiplas instruções separadas por ponto e vírgula
        without_trailing = cleaned.rstrip(";").strip()
        if ";" in without_trailing or "--" in without_trailing or "/*" in without_trailing:
            return {
                "ok": False,
                "error": "ERR_READ_ONLY_SQL_GUARD",
                "message": "Múltiplas instruções ou comentários SQL não são permitidos.",
            }

        # Camada 1: Garante que a query inicia com SELECT e não contém palavras-chave de escrita
        if not re.match(r"^SELECT\b", without_trailing, re.IGNORECASE) or self.FORBIDDEN_KEYWORDS.search(without_trailing):
            return {
                "ok": False,
                "error": "ERR_READ_ONLY_SQL_GUARD",
                "message": "Operação bloqueada: apenas consultas SELECT somente leitura são autorizadas.",
            }

        try:
            cursor = self.conn.execute(without_trailing)
            rows = [dict(row) for row in cursor.fetchmany(50)]
            return {
                "ok": True,
                "row_count": len(rows),
                "rows": rows,
            }
        except sqlite3.Error as exc:
            return {
                "ok": False,
                "error": "ERR_SQL_EXECUTION",
                "message": str(exc),
            }


class SafeASTCalculator:
    """Avaliador matemático seguro via Abstract Syntax Tree (zero `eval`)."""

    BIN_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }

    UNARY_OPS: dict[type[ast.unaryop], Callable[[float], float]] = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }

    SAFE_FUNCS: dict[str, Callable[..., float]] = {
        "sqrt": math.sqrt,
        "abs": abs,
        "round": round,
        "min": min,
        "max": max,
    }

    def evaluate(self, expression: str) -> dict[str, Any]:
        normalized = expression.replace("^", "**").strip()
        if not normalized or len(normalized) > 200:
            return {"ok": False, "error": "ERR_INVALID_EXPRESSION", "message": "Expressão inválida ou longa demais."}

        try:
            tree = ast.parse(normalized, mode="eval")
            value = self._eval_node(tree.body)
            return {
                "ok": True,
                "expression": expression,
                "result": round(float(value), 6),
            }
        except ZeroDivisionError:
            return {"ok": False, "error": "ERR_ZERO_DIVISION", "message": "Divisão por zero detectada."}
        except Exception as exc:
            return {"ok": False, "error": "ERR_AST_EVALUATION", "message": f"Expressão não suportada: {exc}"}

    def _eval_node(self, node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in self.BIN_OPS:
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("Expoente acima do limite permitido (100).")
            return self.BIN_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in self.UNARY_OPS:
            return self.UNARY_OPS[type(node.op)](self._eval_node(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in self.SAFE_FUNCS:
            args = [self._eval_node(arg) for arg in node.args]
            return float(self.SAFE_FUNCS[node.func.id](*args))
        raise ValueError(f"Nó AST não permitido: {type(node).__name__}")


class DocumentSearchEngine:
    """Busca textual ranqueada por relevância em documentos internos."""

    DOCUMENTS = [
        {
            "id": "DOC-01",
            "titulo": "Metas Financeiras e OKRs Q4 2026",
            "categoria": "Financeiro",
            "conteudo": (
                "A meta consolidada de receita bruta para o Q4 2026 é de R$ 1.150.000,00, "
                "com margem bruta mínima alvo de 72%. A região Sudeste responde por 48% da meta trimestral."
            ),
        },
        {
            "id": "DOC-02",
            "titulo": "Política de SLA Enterprise e Penalidades",
            "categoria": "Operações",
            "conteudo": (
                "Chamados críticos P1 possuem SLA máximo de primeira resposta de 15 minutos. "
                "Cada violação de SLA P1 gera crédito contratual automático de R$ 4.500,00 na fatura mensal."
            ),
        },
        {
            "id": "DOC-03",
            "titulo": "Política Comercial e Alçadas de Desconto",
            "categoria": "Comercial",
            "conteudo": (
                "Executivos de contas podem conceder até 12% de desconto sem aprovação. "
                "Descontos entre 12% e 20% exigem aprovação da Diretoria Comercial."
            ),
        },
        {
            "id": "DOC-04",
            "titulo": "Arquitetura e Guardrails do Agente Autônomo",
            "categoria": "Engenharia",
            "conteudo": (
                "Todo agente em produção opera com limite máximo de passos (max_steps <= 8), "
                "timeout por ferramenta e acesso estritamente somente leitura em bancos analíticos."
            ),
        },
    ]

    def search(self, query: str, top_k: int = 3) -> dict[str, Any]:
        tokens = set(re.findall(r"\w+", query.lower()))
        if not tokens:
            return {"ok": True, "matches": []}

        scored: list[tuple[int, dict[str, str]]] = []
        for doc in self.DOCUMENTS:
            haystack = f"{doc['titulo']} {doc['categoria']} {doc['conteudo']}".lower()
            score = sum(3 if tok in doc["titulo"].lower() else 1 for tok in tokens if tok in haystack)
            if score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda item: item[0], reverse=True)
        results = [
            {
                "id": doc["id"],
                "titulo": doc["titulo"],
                "categoria": doc["categoria"],
                "relevancia": score,
                "trecho": doc["conteudo"],
            }
            for score, doc in scored[: max(1, min(top_k, 5))]
        ]
        return {"ok": True, "total_encontrados": len(results), "matches": results}


# ==============================================================================
# 3. ORQUESTRADOR DO LOOP DE FUNCTION CALLING
# ==============================================================================


class ToolCallingAgent:
    """Agente com loop explícito de function calling, guardrails e trace completo."""

    SYSTEM_INSTRUCTION = (
        "Você é um agente analítico corporativo. Responda com precisão usando exclusivamente "
        "dados obtidos pelas ferramentas disponíveis (`consultar_sql`, `buscar_documentos`, `calculadora`). "
        "Tabelas SQL disponíveis: `vendas(id, data, regiao, produto, categoria, receita, custo, unidades)` e "
        "`chamados_sla(id, cliente, severidade, tempo_resposta_min, sla_limite_min, violado)`. "
        "Nunca tente executar comandos de escrita no SQL. Sempre use a calculadora para contas."
    )

    def __init__(self, api_key: str | None = None, model: str = "gemini-2.5-flash") -> None:
        self.model = model
        self.sql_engine = ReadOnlySQLEngine()
        self.calculator = SafeASTCalculator()
        self.doc_search = DocumentSearchEngine()
        self.client = genai.Client(api_key=api_key or os.environ.get("GEMINI_API_KEY")) if genai else None

    def _tool_declarations(self) -> list[Any]:
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="consultar_sql",
                        description="Executa consulta SQL SELECT somente leitura nas tabelas `vendas` e `chamados_sla`.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "query": types.Schema(
                                    type=types.Type.STRING,
                                    description="Consulta SQL iniciando obrigatoriamente com SELECT.",
                                )
                            },
                            required=["query"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="buscar_documentos",
                        description="Busca políticas internas, metas financeiras do Q4, regras de SLA e arquitetura.",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "consulta": types.Schema(
                                    type=types.Type.STRING,
                                    description="Palavras-chave ou pergunta para busca documental.",
                                ),
                                "top_k": types.Schema(
                                    type=types.Type.INTEGER,
                                    description="Quantidade máxima de documentos retornados (1 a 5).",
                                ),
                            },
                            required=["consulta"],
                        ),
                    ),
                    types.FunctionDeclaration(
                        name="calculadora",
                        description="Calcula expressões matemáticas com precisão (suporta +, -, *, /, ^, sqrt, round).",
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "expressao": types.Schema(
                                    type=types.Type.STRING,
                                    description="Expressão matemática válida, ex: '(962000 / 1150000) * 100'",
                                )
                            },
                            required=["expressao"],
                        ),
                    ),
                ]
            )
        ]

    def dispatch_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "consultar_sql":
            return self.sql_engine.execute(str(args.get("query", "")))
        if name == "buscar_documentos":
            return self.doc_search.search(str(args.get("consulta", "")), int(args.get("top_k", 3)))
        if name == "calculadora":
            return self.calculator.evaluate(str(args.get("expressao", "")))
        return {"ok": False, "error": "ERR_UNKNOWN_TOOL", "message": f"Ferramenta desconhecida: {name}"}

    def run(self, prompt: str, max_steps: int = 6) -> AgentTrace:
        if self.client is None:
            raise RuntimeError("Pacote `google-genai` não instalado. Execute: pip install google-genai")

        run_start = time.perf_counter()
        trace = AgentTrace(
            trace_id=f"trc_{int(time.time() * 1000)}",
            prompt=prompt,
            model=self.model,
            max_steps=max_steps,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        contents: list[Any] = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
        config = types.GenerateContentConfig(
            system_instruction=self.SYSTEM_INSTRUCTION,
            tools=self._tool_declarations(),
            temperature=0.1,
        )

        for step_idx in range(1, max_steps + 1):
            step_start = time.perf_counter()
            response = self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )

            candidate = response.candidates[0]
            contents.append(candidate.content)

            function_calls = [
                part.function_call for part in candidate.content.parts if getattr(part, "function_call", None)
            ]
            text_parts = [part.text for part in candidate.content.parts if getattr(part, "text", None)]

            if not function_calls:
                final_text = "\n".join(text_parts).strip()
                trace.steps.append(
                    TraceStep(
                        step=step_idx,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        thought="Síntese final concluída sem necessidade de novas ferramentas.",
                        status="final",
                        latency_ms=round((time.perf_counter() - step_start) * 1000, 2),
                    )
                )
                trace.final_answer = final_text
                trace.status = "completed"
                trace.finished_at = datetime.now(timezone.utc).isoformat()
                trace.total_latency_ms = round((time.perf_counter() - run_start) * 1000, 2)
                return trace

            tool_response_parts: list[Any] = []
            for fc in function_calls:
                args = dict(fc.args) if fc.args else {}
                output = self.dispatch_tool(fc.name, args)
                step_status = (
                    "blocked"
                    if output.get("error") == "ERR_READ_ONLY_SQL_GUARD"
                    else ("ok" if output.get("ok") else "error")
                )

                trace.steps.append(
                    TraceStep(
                        step=step_idx,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        thought=text_parts[0] if text_parts else f"Invocando ferramenta `{fc.name}`",
                        tool_name=fc.name,
                        tool_args=args,
                        tool_output=output,
                        status=step_status,
                        latency_ms=round((time.perf_counter() - step_start) * 1000, 2),
                    )
                )

                tool_response_parts.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response=output,
                    )
                )

            contents.append(types.Content(role="user", parts=tool_response_parts))

        trace.status = "max_steps_exceeded"
        trace.final_answer = (
            f"Execução interrompida pelo guardrail `max_steps={max_steps}` antes da conclusão."
        )
        trace.finished_at = datetime.now(timezone.utc).isoformat()
        trace.total_latency_ms = round((time.perf_counter() - run_start) * 1000, 2)
        return trace


if __name__ == "__main__":
    # Validação local rápida das ferramentas e guardrails (não exige API Key)
    agent = ToolCallingAgent(api_key="demo-offline")
    print("1) SQL SELECT válido:", agent.dispatch_tool("consultar_sql", {"query": "SELECT regiao, SUM(receita) AS total FROM vendas GROUP BY regiao"}))
    print("2) SQL DELETE bloqueado:", agent.dispatch_tool("consultar_sql", {"query": "DELETE FROM vendas WHERE id = 1"}))
    print("3) Busca documental:", agent.dispatch_tool("buscar_documentos", {"consulta": "meta Q4 margem"}))
    print("4) Calculadora AST:", agent.dispatch_tool("calculadora", {"expressao": "(962000 / 1150000) * 100"}))
