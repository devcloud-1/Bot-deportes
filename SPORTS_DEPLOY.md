# Sports Arbitrage Bot — Guía de Deploy en Railway

## Archivos a subir a GitHub

```
sports_trader.py          ← bot principal
requirements.txt          ← (mismo del BTC bot + no cambia)
Procfile                  ← worker: python sports_trader.py --loop
.env.example              ← referencia de variables
```

## Variables en Railway → Settings → Variables

| Variable | Valor |
|---|---|
| SIMMER_API_KEY | tu key de simmer.markets |
| ODDS_API_KEY | c2262242e1326d83c6d8b33d1fca2e62 |
| SPORTS_DAILY_BUDGET | 50.0 |
| SPORTS_MAX_PER_TRADE | 15.0 |
| SPORTS_MIN_EDGE | 0.08 |
| SPORTS_LOOP_SEC | 60 |
| HEALTHCHECK_PORT | 8080 |

## Diferencias vs Bot BTC

| | BTC Bot | Sports Bot |
|---|---|---|
| Fee Polymarket | 10% | ~2% |
| Señal | Momentum precio | Arbitraje vs bookmakers |
| Frecuencia | Cada 15s | Cada 60s |
| Edge mínimo | 0.5% momentum | 8% vs Pinnacle |
| Mercados | Fast markets 5-15min | Partidos reales |
| Spreads | N/A | PROHIBIDOS |

## Estrategia

El bot compara precios de Polymarket contra Pinnacle/DraftKings.
Cuando Polymarket está 8%+ más barato que el bookmaker → compra.

Ejemplo:
- Cavaliers vs Pistons: Pinnacle implica 60% prob Cavaliers
- Polymarket vende YES Cavaliers a 50¢ (50%)
- Edge = 60% - 50% = 10% ✅ → COMPRA

## Sizing (Kelly fraccionario)

El bot usa 25% del Kelly Criterion para sizing conservador:
- Edge 8% → apuesta ~2-3% del budget disponible
- Edge 15% → apuesta ~4-5% del budget disponible
- Nunca más de $15 por trade (configurable)

## Paper Mode vs Live

Sin --live → paper mode automático (seguro)
Con --live → trades reales via Simmer

## The Odds API — límites gratuitos

Plan gratuito: 500 requests/mes
El bot usa ~6 requests por ciclo (1 por deporte)
Con loop de 60s → ~8,640 requests/día en teoría

⚠️  IMPORTANTE: Si usas el plan gratuito, cambia SPORTS_LOOP_SEC=1800
   (ciclo cada 30 minutos) para no agotar el quota.

Plan Basic ($79/mes): 100,000 requests → suficiente para ciclos de 60s
