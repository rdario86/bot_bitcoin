import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta
import time

st.set_page_config(page_title="BOT SMC Sniper (3-Velas FVG) - Bitcoin", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Ajustes SMC Sniper")
    st.markdown("""
    **Nueva Regla de Entrada:**
    - Sweep en NY (08:00 a 10:30).
    - Esperar patrón de **3 velas** con Imbalance (FVG).
    - **Limit:** Borde del Imbalance (Vela 3).
    - **Stop Loss:** Extremo de la Vela 1 del patrón.
    """)
    
    st.subheader("💰 Gestión de Capital")
    capital_inicial = st.number_input("Capital Inicial ($)", min_value=100.0, value=1000.0, step=100.0)
    riesgo_pct = st.selectbox("Riesgo por Operación (%)", options=[1, 2, 3, 4, 5], index=1)
    
    st.divider()
    
    dias_historial = st.slider("Días de Backtesting", 1, 30, 15)
    
    opciones_ratio = {1.0: "1:1", 1.5: "1:1.50", 2.0: "1:2", 2.5: "1:2.50", 3.0: "1:3", 4.0: "1:4"}
    ratio_rr = st.selectbox("Ratio Riesgo/Beneficio", options=list(opciones_ratio.keys()), format_func=lambda x: opciones_ratio[x], index=3) # Por defecto 1:2.5
    
    ejecutar_btn = st.form_submit_button("Ejecutar Backtest Sniper BTC")

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
# 3. MOTOR DE BACKTESTING (PATRÓN 3 VELAS)
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
        fvg_top, fvg_bottom = None, None
        vela_1_time = None
        
        for i in range(len(horario_ny)):
            idx = horario_ny.index[i]
            row = horario_ny.iloc[i]
            
            # Limitar búsqueda de entradas hasta las 10:30 NY
            if idx.time() > pd.to_datetime('10:30').time() and entrada is None:
                break 
                
            # FASE 1: Buscar Toma de Liquidez
            if estado == "Buscando Sweep":
                if row['High'] > liq_max:
                    estado = "Sweep Maximo"
                elif row['Low'] < liq_min:
                    estado = "Sweep Minimo"
            
            # FASE 2: Buscar el patrón de 3 velas (FVG) después del Sweep
            elif estado == "Sweep Maximo":
                if i >= 2:
                    c1 = horario_ny.iloc[i-2]
                    c2 = horario_ny.iloc[i-1]
                    c3 = row
                    
                    # Confirmación de FVG Bajista: Minimo de C1 > Maximo de C3
                    if c1['Low'] > c3['High'] and c1['Close'] > c3['Close']:
                        entrada_limit = c3['High'] # Borde inferior del FVG (Vela 3)
                        stop_loss = c1['High']     # REGLA NUEVA: Máximo de la Vela 1
                        
                        if entrada_limit < stop_loss: # Validar que la estructura sea correcta
                            tp_teorico = entrada_limit - (abs(entrada_limit - stop_loss) * ratio)
                            fvg_top, fvg_bottom = c1['Low'], c3['High']
                            vela_1_time = horario_ny.index[i-2]
                            estado = "Esperando Retroceso Short"
                            
            elif estado == "Sweep Minimo":
                if i >= 2:
                    c1 = horario_ny.iloc[i-2]
                    c2 = horario_ny.iloc[i-1]
                    c3 = row
                    
                    # Confirmación de FVG Alcista: Maximo de C1 < Minimo de C3
                    if c1['High'] < c3['Low'] and c1['Close'] < c3['Close']:
                        entrada_limit = c3['Low']  # Borde superior del FVG (Vela 3)
                        stop_loss = c1['Low']      # REGLA NUEVA: Mínimo de la Vela 1
                        
                        if entrada_limit > stop_loss: # Validar estructura
                            tp_teorico = entrada_limit + (abs(entrada_limit - stop_loss) * ratio)
                            fvg_top, fvg_bottom = c3['Low'], c1['High']
                            vela_1_time = horario_ny.index[i-2]
                            estado = "Esperando Retroceso Long"

            # FASE 3: Activar Orden Limit en el Retroceso
            elif estado == "Esperando Retroceso Short":
                if row['High'] >= stop_loss or row['Low'] <= tp_teorico:
                    estado = "Buscando Sweep" # Setup invalidado antes de entrar
                elif row['High'] >= entrada_limit:
                    entrada, tipo_trade = entrada_limit, 'Short 🔴'
                    break # Entramos!
                    
            elif estado == "Esperando Retroceso Long":
                if row['Low'] <= stop_loss or row['High'] >= tp_teorico:
                    estado = "Buscando Sweep" # Setup invalidado antes de entrar
                elif row['Low'] <= entrada_limit:
                    entrada, tipo_trade = entrada_limit, 'Long 🟢'
                    break # Entramos!

        # FASE 4: Gestión del Trade (hasta las 12:00)
        if entrada is not None:
            riesgo_precio = abs(entrada - stop_loss)
            if riesgo_precio == 0: riesgo_precio = 0.5
            take_profit = tp_teorico
            riesgo_usd = capital_actual * (riesgo_pct / 100)
            resultado, fecha_cierre = "Sin Resolución", None
            
            # Revisamos las velas a partir del momento de entrada
            idx_entrada = horario_ny.index.get_loc(idx)
            df_post = horario_ny.iloc[idx_entrada:]
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
                    'FVG_Top': fvg_top, 'FVG_Bottom': fvg_bottom, 'Vela1_Time': vela_1_time, 
                    'Liq_Max': liq_max, 'Liq_Min': liq_min
                })

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS
# ==========================================
st.title("🎯 BOT SMC Sniper: FVG 3-Velas (Bitcoin)")

df_btc = obtener_datos_bingx(dias_historial)
df_operaciones = ejecutar_backtest(df_btc, ratio_rr, capital_inicial, riesgo_pct)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos.")
elif df_operaciones.empty:
    st.info("No hubo trades que cumplieran el patrón exacto de 3 velas con retroceso al Imbalance.")
else:
    st.subheader("📊 Resumen de Rendimiento SMC Sniper")
    
    total_trades = len(df_operaciones)
    aciertos = len(df_operaciones[df_operaciones['Resultado'].str.contains("Ganancia")])
    fallos = len(df_operaciones[df_operaciones['Resultado'].str.contains("Pérdida")])
    win_rate = (aciertos / total_trades) * 100 if total_trades > 0 else 0
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
    
    st.subheader("🔍 Visualizador del Patrón de Alta Precisión")
    opciones_trades = df_operaciones['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M').tolist()
    trade_seleccionado = st.selectbox("Selecciona un trade para ver la Vela 1, FVG y la Entrada:", opciones_trades)
    
    if trade_seleccionado:
        trade = df_operaciones[df_operaciones['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M') == trade_seleccionado].iloc[0]
        dia_str = trade['Apertura (NY)'].strftime('%Y-%m-%d')
        
        # Gráfica de 06:00 a 12:30 
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
            annotation_text="Hueco FVG", annotation_position="top left"
        )
        
        color = "#00FF00" if "Long" in trade['Tipo'] else "#FF0000"
        simbolo = "triangle-up" if "Long" in trade['Tipo'] else "triangle-down"
        
        # Marcar la "Vela 1" que generó el SL
        fig.add_trace(go.Scatter(
            x=[trade['Vela1_Time']], y=[trade['Stop Loss']], mode='markers', name='Origen Vela 1 (SL)',
            marker=dict(symbol="x", size=10, color="red")
        ))
        
        # Punto de Entrada
        fig.add_trace(go.Scatter(
            x=[trade['Apertura (NY)']], y=[trade['Entrada']], mode='markers', name='Limit Activada (Vela 3)',
            marker=dict(symbol=simbolo, size=15, color=color, line=dict(width=2, color='white'))
        ))
        
        fig.add_hline(y=trade['Stop Loss'], line_dash="solid", line_color="red", annotation_text="Stop Loss (Extremo Vela 1)")
        fig.add_hline(y=trade['Take Profit'], line_dash="solid", line_color="green", annotation_text="Take Profit")
        
        fig.update_layout(
            title=f"Trade {trade['Tipo']} | Entrada Perfecta en FVG | Res: {trade['Resultado']}",
            yaxis_title="Precio Bitcoin (USD)", height=650, xaxis_rangeslider_visible=False, template="plotly_dark"
        )
        st.plotly_chart(fig, use_container_width=True)
