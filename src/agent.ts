/**
 * agente-com-ferramentas | Loop de Function Calling com Guardrails e Trace (TypeScript)
 * Implementação tipada usando `@google/genai`, parser matemático AST e SQL Read-Only Guard.
 */

import { GoogleGenAI, Type, type Content, type FunctionDeclaration, type Part } from "@google/genai";

// ============================================================================
// 1. CONTRATOS E TIPOS DE OBSERVABILIDADE
// ============================================================================

export type ToolName = "consultar_sql" | "buscar_documentos" | "calculadora";
export type StepStatus = "ok" | "blocked" | "error" | "final";

export interface ToolResult {
  ok: boolean;
  error?: string;
  message?: string;
  [key: string]: unknown;
}

export interface TraceStep {
  step: number;
  timestamp: string;
  thought: string;
  toolName?: ToolName | string;
  toolArgs?: Record<string, unknown>;
  toolOutput?: ToolResult;
  status: StepStatus;
  latencyMs: number;
}

export interface AgentTrace {
  traceId: string;
  prompt: string;
  model: string;
  maxSteps: number;
  startedAt: string;
  finishedAt: string;
  totalLatencyMs: number;
  status: "completed" | "max_steps_exceeded" | "error";
  finalAnswer: string;
  steps: TraceStep[];
}

export interface AgentOptions {
  apiKey?: string;
  model?: string;
  maxSteps?: number;
}

// ============================================================================
// 2. GUARDRAILS E FERRAMENTAS DETERMINÍSTICAS
// ============================================================================

const FORBIDDEN_SQL_REGEX =
  /\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|ATTACH|DETACH|PRAGMA|GRANT|REVOKE|EXEC)\b/i;

interface VendaRow {
  id: number;
  data: string;
  regiao: string;
  produto: string;
  categoria: string;
  receita: number;
  custo: number;
  unidades: number;
}

const VENDAS_DATA: VendaRow[] = [
  { id: 1, data: "2026-10-03", regiao: "Sudeste", produto: "Enterprise AI Suite", categoria: "SaaS", receita: 185000, custo: 42000, unidades: 5 },
  { id: 2, data: "2026-10-12", regiao: "Sul", produto: "DataGuard SQL Proxy", categoria: "Seguranca", receita: 92000, custo: 21000, unidades: 4 },
  { id: 3, data: "2026-10-19", regiao: "Sudeste", produto: "Observability Cloud", categoria: "DevOps", receita: 134000, custo: 35000, unidades: 6 },
  { id: 4, data: "2026-11-02", regiao: "Nordeste", produto: "Enterprise AI Suite", categoria: "SaaS", receita: 148000, custo: 33600, unidades: 4 },
  { id: 5, data: "2026-11-08", regiao: "Centro-Oeste", produto: "DataGuard SQL Proxy", categoria: "Seguranca", receita: 69000, custo: 15750, unidades: 3 },
  { id: 6, data: "2026-11-14", regiao: "Sul", produto: "Observability Cloud", categoria: "DevOps", receita: 112000, custo: 29000, unidades: 5 },
  { id: 7, data: "2026-11-16", regiao: "Sudeste", produto: "Enterprise AI Suite", categoria: "SaaS", receita: 222000, custo: 50400, unidades: 6 },
];

const KNOWLEDGE_BASE = [
  {
    id: "DOC-01",
    titulo: "Metas Financeiras e OKRs Q4 2026",
    categoria: "Financeiro",
    conteudo: "A meta consolidada de receita bruta para o Q4 2026 é de R$ 1.150.000,00, com margem bruta mínima alvo de 72%.",
  },
  {
    id: "DOC-02",
    titulo: "Política de SLA Enterprise e Penalidades",
    categoria: "Operações",
    conteudo: "Chamados críticos P1 possuem SLA máximo de primeira resposta de 15 minutos. Cada violação gera crédito de R$ 4.500,00.",
  },
  {
    id: "DOC-03",
    titulo: "Arquitetura e Guardrails do Agente Autônomo",
    categoria: "Engenharia",
    conteudo: "Todo agente opera com max_steps <= 8 e acesso estritamente somente leitura em bases analíticas.",
  },
];

export function executeReadOnlySql(query: string): ToolResult {
  const cleaned = query.trim().replace(/;+$/, "");
  if (!cleaned) {
    return { ok: false, error: "ERR_EMPTY_QUERY", message: "A consulta SQL está vazia." };
  }
  if (cleaned.includes(";") || cleaned.includes("--") || cleaned.includes("/*")) {
    return {
      ok: false,
      error: "ERR_READ_ONLY_SQL_GUARD",
      message: "Múltiplas instruções ou comentários SQL bloqueados pelo guardrail.",
    };
  }
  if (!/^SELECT\b/i.test(cleaned) || FORBIDDEN_SQL_REGEX.test(cleaned)) {
    return {
      ok: false,
      error: "ERR_READ_ONLY_SQL_GUARD",
      message: "Operação bloqueada: apenas consultas SELECT somente leitura são permitidas.",
    };
  }

  // Exemplo de execução analítica segura sobre o dataset tipado
  const byRegiao = /GROUP\s+BY\s+regiao/i.test(cleaned);
  if (byRegiao) {
    const grouped = VENDAS_DATA.reduce<Record<string, { regiao: string; receita_total: number; unidades: number }>>(
      (acc, row) => {
        if (!acc[row.regiao]) {
          acc[row.regiao] = { regiao: row.regiao, receita_total: 0, unidades: 0 };
        }
        acc[row.regiao].receita_total += row.receita;
        acc[row.regiao].unidades += row.unidades;
        return acc;
      },
      {}
    );
    const rows = Object.values(grouped);
    return { ok: true, rowCount: rows.length, rows };
  }

  return { ok: true, rowCount: VENDAS_DATA.length, rows: VENDAS_DATA };
}

export function searchDocuments(consulta: string, topK = 3): ToolResult {
  const tokens = consulta.toLowerCase().split(/\W+/).filter(Boolean);
  const matches = KNOWLEDGE_BASE.map((doc) => {
    const text = `${doc.titulo} ${doc.categoria} ${doc.conteudo}`.toLowerCase();
    const score = tokens.reduce((sum, token) => sum + (text.includes(token) ? 1 : 0), 0);
    return { ...doc, relevancia: score };
  })
    .filter((item) => item.relevancia > 0)
    .sort((a, b) => b.relevancia - a.relevancia)
    .slice(0, Math.max(1, Math.min(topK, 5)));

  return { ok: true, totalEncontrados: matches.length, matches };
}

/**
 * Parser matemático descendente recursivo (sem `eval` ou `new Function`).
 */
export function evaluateSafeMath(expression: string): ToolResult {
  const src = expression.replace(/\s+/g, "");
  if (!src || src.length > 200) {
    return { ok: false, error: "ERR_INVALID_EXPRESSION", message: "Expressão inválida." };
  }

  let pos = 0;

  function parseExpression(): number {
    let value = parseTerm();
    while (pos < src.length && (src[pos] === "+" || src[pos] === "-")) {
      const op = src[pos++];
      const right = parseTerm();
      value = op === "+" ? value + right : value - right;
    }
    return value;
  }

  function parseTerm(): number {
    let value = parseFactor();
    while (pos < src.length && (src[pos] === "*" || src[pos] === "/" || src[pos] === "%")) {
      const op = src[pos++];
      const right = parseFactor();
      if (op === "*") value *= right;
      else if (op === "/") {
        if (right === 0) throw new Error("Divisão por zero.");
        value /= right;
      } else {
        value %= right;
      }
    }
    return value;
  }

  function parseFactor(): number {
    if (src[pos] === "-") {
      pos++;
      return -parseFactor();
    }
    if (src[pos] === "(") {
      pos++;
      const val = parseExpression();
      if (src[pos] !== ")") throw new Error("Parêntese não fechado.");
      pos++;
      return val;
    }
    const start = pos;
    while (pos < src.length && /[0-9.]/.test(src[pos])) pos++;
    if (start === pos) throw new Error(`Token inesperado em '${src[pos] ?? "EOF"}'`);
    const num = Number(src.slice(start, pos));
    if (Number.isNaN(num)) throw new Error("Número inválido.");
    return num;
  }

  try {
    const result = parseExpression();
    if (pos !== src.length) throw new Error("Caracteres excedentes na expressão.");
    return { ok: true, expression, result: Number(result.toFixed(6)) };
  } catch (err) {
    return {
      ok: false,
      error: "ERR_AST_EVALUATION",
      message: err instanceof Error ? err.message : "Erro desconhecido na calculadora.",
    };
  }
}

// ============================================================================
// 3. DECLARAÇÕES DE FERRAMENTAS E LOOP DO AGENTE
// ============================================================================

const TOOL_DECLARATIONS: FunctionDeclaration[] = [
  {
    name: "consultar_sql",
    description: "Executa consultas SQL SELECT somente leitura na base analítica `vendas`.",
    parameters: {
      type: Type.OBJECT,
      properties: {
        query: { type: Type.STRING, description: "Consulta SQL iniciando obrigatoriamente com SELECT." },
      },
      required: ["query"],
    },
  },
  {
    name: "buscar_documentos",
    description: "Busca documentos internos de metas Q4, SLA e políticas corporativas.",
    parameters: {
      type: Type.OBJECT,
      properties: {
        consulta: { type: Type.STRING, description: "Termos de busca." },
        top_k: { type: Type.INTEGER, description: "Número máximo de resultados." },
      },
      required: ["consulta"],
    },
  },
  {
    name: "calculadora",
    description: "Avalia expressões matemáticas com segurança (sem eval).",
    parameters: {
      type: Type.OBJECT,
      properties: {
        expressao: { type: Type.STRING, description: "Expressão aritmética, ex: '(962000 / 1150000) * 100'" },
      },
      required: ["expressao"],
    },
  },
];

function dispatchTool(name: string, args: Record<string, unknown>): ToolResult {
  switch (name) {
    case "consultar_sql":
      return executeReadOnlySql(String(args.query ?? ""));
    case "buscar_documentos":
      return searchDocuments(String(args.consulta ?? ""), Number(args.top_k ?? 3));
    case "calculadora":
      return evaluateSafeMath(String(args.expressao ?? ""));
    default:
      return { ok: false, error: "ERR_UNKNOWN_TOOL", message: `Ferramenta desconhecida: ${name}` };
  }
}

export async function runToolCallingAgent(prompt: string, options: AgentOptions = {}): Promise<AgentTrace> {
  const model = options.model ?? "gemini-2.5-flash";
  const maxSteps = options.maxSteps ?? 6;
  const ai = new GoogleGenAI({ apiKey: options.apiKey ?? process.env.GEMINI_API_KEY });

  const runStart = performance.now();
  const trace: AgentTrace = {
    traceId: `trc_${Date.now()}`,
    prompt,
    model,
    maxSteps,
    startedAt: new Date().toISOString(),
    finishedAt: "",
    totalLatencyMs: 0,
    status: "completed",
    finalAnswer: "",
    steps: [],
  };

  const contents: Content[] = [{ role: "user", parts: [{ text: prompt }] }];

  for (let step = 1; step <= maxSteps; step++) {
    const stepStart = performance.now();
    const response = await ai.models.generateContent({
      model,
      contents,
      config: {
        systemInstruction:
          "Você é um agente corporativo. Use `consultar_sql`, `buscar_documentos` e `calculadora` para responder com precisão.",
        tools: [{ functionDeclarations: TOOL_DECLARATIONS }],
        temperature: 0.1,
      },
    });

    const candidateContent = response.candidates?.[0]?.content;
    if (!candidateContent) {
      trace.status = "error";
      trace.finalAnswer = "Resposta vazia do modelo.";
      break;
    }

    contents.push(candidateContent);
    const parts = candidateContent.parts ?? [];
    const functionCalls = parts.filter((p) => p.functionCall).map((p) => p.functionCall!);
    const textReply = parts
      .filter((p) => p.text)
      .map((p) => p.text)
      .join("\n")
      .trim();

    if (functionCalls.length === 0) {
      trace.steps.push({
        step,
        timestamp: new Date().toISOString(),
        thought: "Síntese final concluída.",
        status: "final",
        latencyMs: Number((performance.now() - stepStart).toFixed(2)),
      });
      trace.finalAnswer = textReply;
      trace.status = "completed";
      trace.finishedAt = new Date().toISOString();
      trace.totalLatencyMs = Number((performance.now() - runStart).toFixed(2));
      return trace;
    }

    const responseParts: Part[] = [];
    for (const fc of functionCalls) {
      const toolName = fc.name ?? "unknown";
      const args = (fc.args ?? {}) as Record<string, unknown>;
      const output = dispatchTool(toolName, args);

      trace.steps.push({
        step,
        timestamp: new Date().toISOString(),
        thought: textReply || `Executando ${toolName}`,
        toolName,
        toolArgs: args,
        toolOutput: output,
        status: output.error === "ERR_READ_ONLY_SQL_GUARD" ? "blocked" : output.ok ? "ok" : "error",
        latencyMs: Number((performance.now() - stepStart).toFixed(2)),
      });

      responseParts.push({
        functionResponse: {
          name: toolName,
          response: output,
        },
      });
    }

    contents.push({ role: "user", parts: responseParts });
  }

  trace.status = "max_steps_exceeded";
  trace.finalAnswer = `Execução interrompida pelo guardrail maxSteps=${maxSteps}.`;
  trace.finishedAt = new Date().toISOString();
  trace.totalLatencyMs = Number((performance.now() - runStart).toFixed(2));
  return trace;
}
