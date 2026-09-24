import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta
import time

st.set_page_config(page_title="BOT Smart Money (Imbalance) - Bitcoin", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Ajustes Avanzados SMC")
    st.markdown("""
    **Reglas Actualizadas:**
    - **Búsqueda:** 08:00 a 10:30 NY.
    - **Entrada:** Orden Limit en el último Imbalance (FVG) tras el ChoCh.
    - **Cierre Forzado:** 12:00 NY.
    """)
    
    st.subheader("💰 Gestión de Capital")
    capital_inicial = st.number_input("Capital Inicial ($)", min_value=100.0, value=1000.0, step=100.0)
    riesgo_pct = st.selectbox("Riesgo por Operación (%)", options=[1, 2, 3, 4, 5], index=1)
    
    st.divider()
    
    dias_historial = st.slider("Días de Backtesting", 1, 30, 15)
    
    opciones_ratio = {1.0: "1:1", 1.5: "1:1.50", 2.0: "1:2", 2.5: "1:2.50", 3.0: "1:3"}
    ratio_rr = st.selectbox("Ratio Riesgo/Beneficio", options=list(opciones_ratio.keys()), format_func=lambda x: opciones_ratio[x], index=2)
    
    ejecutar_btn = st.form_submit_button("Ejecutar Backtest SMC BTC")

# ==========================================
# 2. CONEXIÓN A BINGX (1m)
# ==========================================
@st.cache_data(ttl=300, show_spinner="Descargando histórico de Bitcoin (BingX 1m)...")
def obtener_datos_bingx(dias):
    try:
        exchange = ccxt.bingx({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
        ahora = pd.Timestamp.utcnow()
        inicio = ahora - pd.Timedelta(days=dias)
        since = exchange.parse8601(inicio.isoformat())
        
        todas_las_velas = []
        limite_velas = 1000 
        
        while True:
            try:
                velas = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1m', since=since, limit=limite_velas)
                if not velas: break
                todas_las_velas.extend(velas)
                since = velas[-1][0] + 60000 
                if len(velas) < limite_velas: break 
                time.sleep(0.1) 
            except Exception:
                time.sleep(1)
                
        if not todas_las_velas: return pd.DataFrame()
            
        df = pd.DataFrame(todas_las_velas, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Timestamp', inplace=True)
        
        df.index = df.index.tz_localize('UTC').tz_convert('America/New_York')
        df = df[~df.index.duplicated(keep='first')]
        df['Date'] = df.index.date
        
        return df
    except Exception as e:
        st.error(f"Error de conexión: {e}")
        return pd.DataFrame()

# ==========================================
# 3. MOTOR DE BACKTESTING (CHOCH + IMBALANCE)
# ==========================================
def ejecutar_backtest(df, ratio, capital_inicial, riesgo_pct):
    operaciones = []
    if df.empty: return pd.DataFrame(operaciones)
        
    df_calc = df.copy()
    fechas = df_calc['Date'].unique()
    capital_actual = capital_inicial
    
    for fecha in fechas:
        # 1. Definir Liquidez Previa (00:00 a 08:00 NY)
        hora_inicio_liq = pd.to_datetime('00:00').time()
        hora_fin_liq = pd.to_datetime('08:00').time()
        velas_liq = df_calc.loc[(df_calc['Date'] == fecha) & (df_calc.index.time >= hora_inicio_liq) & (df_calc.index.time < hora_fin_liq)]
        
        if len(velas_liq) < 60: continue 
            
        liq_max = velas_liq['High'].max()
        liq_min = velas_liq['Low'].min()
        
        # 2. Killzone NY (08:00 a 10:30 NY)
        horario_ny = df_calc.loc[(df_calc['Date'] == fecha) & (df_calc.index.time >= hora_fin_liq)]
        
        estado = "Buscando Sweep"
        tipo_trade, entrada, stop_loss = None, None, None
        entrada_limit, tp_teorico = None, None
        sweep_peak, sweep_peak_idx = None, None
        choch_level = None
        fvg_top, fvg_bottom = None, None
        
        for idx, row in horario_ny.iterrows():
            # Limitar búsqueda y entradas hasta las 10:30 NY
            if idx.time() > pd.to_datetime('10:30').time():
                break 
                
            # FASE 1: Buscar Sweep
            if estado == "Buscando Sweep":
                if row['High'] > liq_max:
                    estado = "Sweep Maximo"
                    sweep_peak, sweep_peak_idx = row['High'], idx
                    velas_previas = horario_ny.loc[:idx].tail(15)
                    choch_level = velas_previas['Low'].min()
                
                elif row['Low'] < liq_min:
                    estado = "Sweep Minimo"
                    sweep_peak, sweep_peak_idx = row['Low'], idx
                    velas_previas = horario_ny.loc[:idx].tail(15)
                    choch_level = velas_previas['High'].max()
            
            # FASE 2: Confirmar ChoCh y buscar IMBALANCE
            elif estado == "Sweep Maximo":
                if row['High'] > sweep_peak:
                    sweep_peak, sweep_peak_idx = row['High'], idx
                
                if row['Close'] < choch_level:
                    # ChoCh Confirmado! Buscar FVG en el impulso bajista
                    df_impulso = horario_ny.loc[sweep_peak_idx:idx]
                    
                    for i in range(2, len(df_impulso)):
                        v1, v3 = df_impulso.iloc[i-2], df_impulso.iloc[i]
                        if v1['Low'] > v3['High']: # FVG Bajista
                            entrada_limit = v3['High'] # Límite en el borde inferior del hueco
                            fvg_bottom, fvg_top = v3['High'], v1['Low']
                    
                    if entrada_limit is not None:
                        stop_loss = sweep_peak
                        tp_teorico = entrada_limit - (abs(entrada_limit - stop_loss) * ratio)
                        estado = "Esperando Retroceso Short"
                    else:
                        estado = "Buscando Sweep" # Si no dejó imbalance, descartamos
                        
            elif estado == "Sweep Minimo":
                if row['Low'] < sweep_peak:
                    sweep_peak, sweep_peak_idx = row['Low'], idx
                
                if row['Close'] > choch_level:
                    # ChoCh Confirmado! Buscar FVG en el impulso alcista
                    df_impulso = horario_ny.loc[sweep_peak_idx:idx]
                    
                    for i in range(2, len(df_impulso)):
                        v1, v3 = df_impulso.iloc[i-2], df_impulso.iloc[i]
                        if v1['High'] < v3['Low']: # FVG Alcista
                            entrada_limit = v3['Low'] # Límite en el borde superior del hueco
                            fvg_top, fvg_bottom = v3['Low'], v1['High']
                    
                    if entrada_limit is not None:
                        stop_loss = sweep_peak
                        tp_teorico = entrada_limit + (abs(entrada_limit - stop_loss) * ratio)
                        estado = "Esperando Retroceso Long"
                    else:
                        estado = "Buscando Sweep"

            # FASE 3: Esperar que el precio active la Orden Limit
            elif estado == "Esperando Retroceso Short":
                if row['High'] >= entrada_limit:
                    entrada, tipo_trade = entrada_limit, 'Short 🔴'
                    break # Entramos al trade!
                elif row['High'] >= stop_loss or row['Low'] <= tp_teorico:
                    estado = "Buscando Sweep" # Invalidado (Rompió SL o llegó al TP antes de entrar)
                    
            elif estado == "Esperando Retroceso Long":
                if row['Low'] <= entrada_limit:
                    entrada, tipo_trade = entrada_limit, 'Long 🟢'
                    break # Entramos al trade!
                elif row['Low'] <= stop_loss or row['High'] >= tp_teorico:
                    estado = "Buscando Sweep"

        # FASE 4: Gestión del Trade (hasta las 12:00)
        if entrada is not None:
            riesgo_precio = abs(entrada - stop_loss)
            if riesgo_precio == 0: riesgo_precio = 0.5
            take_profit = tp_teorico
            riesgo_usd = capital_actual * (riesgo_pct / 100)
            resultado, fecha_cierre = "Sin Resolución", None
            
            # Revisamos las velas a partir del momento de entrada
            df_post = df_calc.loc[idx:]
            limite_cierre = idx.replace(hour=12, minute=0, second=0)
            
            for jdx, vela in df_post.iterrows():
                # Cierre Forzado a las 12:00 NY
                if jdx >= limite_cierre:
                    precio_cierre = vela['Open']
                    dist = (entrada - precio_cierre) if "Short" in tipo_trade else (precio_cierre - entrada)
                    pnl_usd = (dist / riesgo_precio) * riesgo_usd
                    resultado = "Ganancia (12:00) ⏱️✅" if pnl_usd > 0 else "Pérdida (12:00) ⏱️❌"
                    fecha_cierre = jdx
                    break
                
                # Cierre por SL
                if ("Long" in tipo_trade and vela['Low'] <= stop_loss) or ("Short" in tipo_trade and vela['High'] >= stop_loss):
                    resultado, pnl_usd = "Pérdida (SL) ❌", -riesgo_usd
                    fecha_cierre = jdx
                    break
                
                # Cierre por TP
                elif ("Long" in tipo_trade and vela['High'] >= take_profit) or ("Short" in tipo_trade and vela['Low'] <= take_profit):
                    resultado, pnl_usd = "Ganancia (TP) ✅", riesgo_usd * ratio
                    fecha_cierre = jdx
                    break
            
            if fecha_cierre is not None:
                capital_actual += pnl_usd
                operaciones.append({
                    'Apertura (NY)': idx, 'Cierre (NY)': fecha_cierre, 'Tipo': tipo_trade,
                    'Entrada': entrada, 'Stop Loss': stop_loss, 'Take Profit': take_profit,
                    'Resultado': resultado, 'PnL ($)': pnl_usd, 'Balance': capital_actual,
                    'FVG_Top': fvg_top, 'FVG_Bottom': fvg_bottom, 'Liq_Max': liq_max, 'Liq_Min': liq_min
                })

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS
# ==========================================
st.title("📉 BOT SMC: Barrido + Imbalance (08:00 a 10:30)")

df_btc = obtener_datos_bingx(dias_historial)
df_operaciones = ejecutar_backtest(df_btc, ratio_rr, capital_inicial, riesgo_pct)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos.")
elif df_operaciones.empty:
    st.info("No hubo trades que cumplieran exactamente las reglas (ChoCh + Imbalance) antes de las 10:30.")
else:
    st.subheader("📊 Resumen de Rendimiento SMC")
    
    total_trades = len(df_operaciones)
    aciertos = len(df_operaciones[df_operaciones['Resultado'].str.contains("Ganancia")])
    fallos = len(df_operaciones[df_operaciones['Resultado'].str.contains("Pérdida")])
    win_rate = (aciertos / total_trades) * 100
    ganancia_neta = df_operaciones['PnL ($)'].sum()
    balance_final = capital_inicial + ganancia_neta

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Trades", total_trades)
    c2.metric("Aciertos ✅", aciertos)
    c3.metric("Fallos ❌", fallos)
    c4.metric("% Win Rate", f"{win_rate:.1f}%")
    
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Capital Inicial", f"${capital_inicial:,.2f}")
    c6.metric("Balance Proyectado", f"${balance_final:,.2f}")
    c7.metric("PnL Neto ($)", f"${ganancia_neta:,.2f}", delta_color="normal" if ganancia_neta >= 0 else "inverse")
    c8.metric("Rentabilidad (%)", f"{(ganancia_neta / capital_inicial * 100):.2f}%")

    st.divider()
    
    st.subheader("🔍 Visualizador: Operando el Imbalance")
    opciones_trades = df_operaciones['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M').tolist()
    trade_seleccionado = st.selectbox("Selecciona un trade para ver el FVG:", opciones_trades)
    
    if trade_seleccionado:
        trade = df_operaciones[df_operaciones['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M') == trade_seleccionado].iloc[0]
        dia_str = trade['Apertura (NY)'].strftime('%Y-%m-%d')
        
        # Gráfica de 06:00 a 12:30 para ver todo el contexto y el cierre
        df_dia = df_btc.loc[f"{dia_str} 06:00:00":f"{dia_str} 12:30:00"]
        
        fig = go.Figure(data=[go.Candlestick(
            x=df_dia.index, open=df_dia['Open'], high=df_dia['High'], low=df_dia['Low'], close=df_dia['Close'], name='BTC/USDT'
        )])
        
        # Líneas de Liquidez
        fig.add_hline(y=trade['Liq_Max'], line_dash="dash", line_color="orange", annotation_text="Max (Asia/Londres)")
        fig.add_hline(y=trade['Liq_Min'], line_dash="dash", line_color="orange", annotation_text="Min (Asia/Londres)")
        
        # Resaltar el Imbalance (FVG)
        fig.add_hrect(
            y0=trade['FVG_Bottom'], y1=trade['FVG_Top'], line_width=0, fillcolor="rgba(255, 255, 0, 0.2)",
            annotation_text="Imbalance (FVG)", annotation_position="top left"
        )
        
        # Puntos de Entrada y Salida
        color = "#00FF00" if "Long" in trade['Tipo'] else "#FF0000"
        simbolo = "triangle-up" if "Long" in trade['Tipo'] else "triangle-down"
        
        fig.add_trace(go.Scatter(
            x=[trade['Apertura (NY)']], y=[trade['Entrada']], mode='markers', name='Orden Limit Activada',
            marker=dict(symbol=simbolo, size=15, color=color, line=dict(width=2, color='white'))
        ))
        
        fig.add_hline(y=trade['Stop Loss'], line_dash="solid", line_color="red", annotation_text="Stop Loss")
        fig.add_hline(y=trade['Take Profit'], line_dash="solid", line_color="green", annotation_text="Take Profit")
        
        fig.update_layout(
            title=f"Trade {trade['Tipo']} | Entrando en FVG | Res: {trade['Resultado']}",
            yaxis_title="Precio Bitcoin (USD)", height=650, xaxis_rangeslider_visible=False, template="plotly_dark"
        )
        st.plotly_chart(fig, use_container_width=True)
