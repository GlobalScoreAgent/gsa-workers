# Por qué descubrimos ERC-20 y cómo los valoramos

Documento de negocio (con anclas técnicas) sobre el discovery de holdings fungibles y la cascada de precios. Catálogo operativo: [PROCESSES.md](./PROCESSES.md).

## El problema de negocio

Global Score Agent necesita **saber qué tokens tiene realmente cada wallet** en cada red, no solo el balance nativo (ETH, POL, BNB…).

Sin los ERC-20 no podemos:

- Estimar el **valor de portafolio** de un agente / owner
- Alimentar pilares e índices que dependen de holdings (calidad, concentración, exposición)
- Distinguir una wallet “vacía” de una con decenas de tokens illiquids o bluechips
- Construir la base para precios, spam detection y, más adelante, otras posiciones DeFi

El mercado (p. ej. Zerion) resuelve esto como **posiciones fungibles** por asset. Nosotros materializamos algo equivalente en Postgres: primero la **lista de contratos con balance**, luego montos y USD.

### Por qué no bastaba lo anterior

En EVM **no hay una llamada estándar** “dame todos mis ERC-20”. Cada token es un contrato distinto; hay que conocer la address o usar un proveedor que ya indexó los holdings.

Un enfoque de “lista fija de tokens populares” (como el scanner standalone antiguo) es barato pero **sesgado**: omite casi todo el long-tail y no refleja el portafolio real del agente. Para el producto necesitábamos **cobertura amplia por wallet**, no un catálogo cerrado.

## Qué decidimos: Alchemy Free + `getTokenBalances`

Alchemy ofrece un endpoint de Token API (`alchemy_getTokenBalances` con tipo `"erc20"`) que devuelve el **snapshot de ERC-20 con balance actual** de una address, ya indexado.

### Por qué encaja en negocio

| Criterio | Qué ganamos |
|---|---|
| **Volumen** | El free tier (key dedicada `ALCHEMY_FREE_KEY`) permite procesar muchas wallets × chains en corridas batch de varias horas, sin quemar el key de RPC de los jobs de balance/nonce |
| **Cobertura** | No limitamos a una lista interna de tokens: descubrimos lo que Alchemy indexa con balance > 0 |
| **Objetivo correcto** | Preguntamos “¿qué tiene **ahora**?”, no “¿todo lo que alguna vez tocó?” — es lo que necesitamos para un snapshot de portafolio |
| **Coste vs alternativas** | Paginar todo el historial de Transfer (`getAssetTransfers`) o unir N explorers es más lento, más caro y más frágil; para el goal comercial del fill inicial no aporta |

En la práctica: un worker claim (`wallet_token_contracts_discovery`) recorre `wallet_transactions` elegibles, llama Alchemy Free por chain, y guarda solo las **addresses** con balance > 0 en `wallets.wallet_token_contracts`. Eso es el inventario; no todavía el precio.

La key Free se **separa** del `ALCHEMY_KEY` de los workers de nonce/balance para no mezclar cuota de RPC de producción con el volumen alto del discovery de tokens.

## Qué hicimos después: precios con fallback

Tener el contrato **no alcanza**: el producto necesita **USD** (o saber que no hay precio de mercado usable).

### Origen primario — DeFiLlama (al armar el portafolio)

Cuando descubrimos montos (`wallet_token_portfolio_discovery`), el primer precio viene de **DeFiLlama**.

- Si Llama responde con precio usable → posición `priced`
- Si no → dejamos la fila marcada como pendiente de precio (`has_price_error`), clasificando spam vs unpriced

Llama es buen “default” (nativos + tokens listados), pero **falla sistemáticamente en el long-tail**: tokens nuevos, clones, illiquidos, o sin mapping en Llama. Eso no es un bug puntual: es el perfil real de muchas wallets de agentes.

Sin un segundo paso, el backlog de “sin precio” se quedaba estancado o se reintentaría siempre la misma fuente.

### Orígenes 2 y 3 — DexScreener y luego CoinGecko

Implementamos un job aparte (`token_prices_import`) que solo mira ERC-20 **aún sin precio usable**:

1. **DexScreener** — precio de mercado on-chain si hay par con liquidez suficiente (útil para tokens que viven en DEX y Llama no conoce)
2. **CoinGecko** — listado más amplio por contract address para lo que Dex no resolvió
3. Cache en `wallets.token_prices` (TTL) para no repartir las mismas consultas cada corrida
4. Aplicar hits a las positions; si **ambas** fallan (después de que Llama ya falló), marcar el token como **known-unknown** (`quality_reason = unknown_token_dex_coingecko_defillama`) y sacarlo de la cola de enrich

### Por qué este orden (negocio)

```text
DeFiLlama (rápido, amplio en bluechips)
    → si falla: DexScreener (liquidez real en DEX)
        → si falla: CoinGecko (catálogo/API)
            → si falla: “sin mercado” (dejar de insistir)
```

- **No** mezclamos las tres fuentes dentro del discovery de contratos: discovery debe ser barato y estable; el precio es un problema de datos de mercado.
- Priorizamos Dex antes que CG para **aprovechar liquidez on-chain** y cuidar tasa/créditos de CoinGecko.
- Cerrar el triángulo Llama → Dex → CG evita **reprocesar eternamente** tokens que nunca van a tener precio de mercado; el sistema puede seguir con el universo restante.

## Resultado en cadena (vista producto)

```text
1. Inventario ERC-20 (Alchemy Free)
      → wallet_token_contracts

2. Montos + primer precio (Llama)
      → wallet_token_positions

3. Fallback de precio (Dex → CoinGecko) + cierre de desconocidos
      → token_prices + positions actualizadas
```

Con eso el producto pasa de “solo native / lista corta” a **portafolio fungible descubierto a escala**, con valoración progresiva y una política clara de qué hacer cuando el mercado no cotiza el token.

## Dónde está el detalle operativo

| Tema | Doc / código |
|---|---|
| Worker inventory | [`wallet_token_contracts_discovery`](../workers/wallet_token_contracts_discovery/README.md) |
| Worker montos + Llama | [`wallet_token_portfolio_discovery`](../workers/wallet_token_portfolio_discovery/README.md) |
| Worker Dex/CG | [`token_prices_import`](../workers/token_prices_import/README.md) |
| Pipelines / cron / secrets | [PROCESSES.md](./PROCESSES.md), [ARCHITECTURE.md](./ARCHITECTURE.md), [SUPABASE.md](./SUPABASE.md) |
