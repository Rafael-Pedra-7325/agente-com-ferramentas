# agente-com-ferramentas

Agente autônomo com loop explícito de **Function Calling**, ferramentas determinísticas (**busca documental**, **SQL somente leitura** e **calculadora via AST**), **limite máximo de passos (`max_steps`)** e **inspetor visual de trace** em tempo real.

## Estrutura do Projeto

```text
agente-com-ferramentas/
├── index.html          # Playground visual e inspetor de trace (deploy estático)
├── src/
│   ├── agent.py        # Loop de execução, SQLite authorizer e AST calc em Python
│   └── agent.ts        # Loop de execução tipado e parser descendente em TypeScript
├── .gitignore
├── LICENSE
└── README.md
```

## Destaques de Engenharia & Guardrails

1. **SQL Somente Leitura em Dupla Camada (`consultar_sql`)**:
   - **Camada Léxica**: Bloqueia comandos destrutivos (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`, `PRAGMA`), múltiplas queries (`;`) e comentários SQL.
   - **Camada de Engine (`src/agent.py`)**: Utiliza `sqlite3.Connection.set_authorizer()` permitindo exclusivamente `SQLITE_SELECT`, `SQLITE_READ` e `SQLITE_FUNCTION`.
2. **Calculadora Segura via AST (`calculadora`)**:
   - Avalia expressões matemáticas percorrendo a árvore sintática (`ast.parse` em Python e parser descendente recursivo em TypeScript/JS), eliminando riscos de `eval()`.
3. **Controle de Custo e Loop (`max_steps`)**:
   - Interrompe execuções ciclicas ou excessivamente longas ao atingir o teto configurado (`max_steps`), registrando o status `max_steps_exceeded` no trace.
4. **Observabilidade Passo a Passo (`AgentTrace`)**:
   - Registra pensamento/raciocínio, argumentos JSON da chamada, saída da ferramenta, status (`ok`, `blocked`, `error`, `final`) e latência em milissegundos por etapa, com exportação em `.json`.

## Como Executar

### 1. Playground Web (Zero Dependências)
Abra `index.html` diretamente no navegador ou publique em qualquer hospedagem estática (Vercel, Netlify, GitHub Pages). Funciona imediatamente no **Modo Sandbox** ou conectado à **Gemini 2.5 Flash API**.

### 2. Backend Python
```bash
pip install google-genai
export GEMINI_API_KEY="sua-chave-aqui"
python src/agent.py
```

### 3. Backend TypeScript
```bash
npm install @google/genai
export GEMINI_API_KEY="sua-chave-aqui"
npx tsx src/agent.ts
```
