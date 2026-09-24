import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta
import time

st.set_page_config(page_title="BOT SMC Sniper (PDH/PDL) - Bitcoin", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ SMC: PDH / PDL Sweep")
    st.markdown("""
    **Liquidez Mayor:**
    - **PDH / PDL:** Máximo y Mínimo del día anterior.
    - **Sweep:** Barrido en NY (08:00 a 10:30).
    - **Gatillo:** FVG de 3 Velas (Entrada al toque).
    - **Stop Loss:** Extremo de la Vela 1.
    """)
    
    st.subheader("💰 Gestión de Capital")
    capital_inicial = st.number_input("Capital Inicial ($)", min_value=100.0, value=1000.0, step=100.0)
    riesgo_pct = st.selectbox("Riesgo por Operación (%)", options=[1, 2, 3, 4, 5], index=1)
    
    st.divider()
    
    # Aumentamos un poco los días por defecto ya que ahora dependemos del día anterior
    dias_historial = st.slider("Días de Backtesting", 2, 30, 20)
    
    opciones_ratio = {1.0: "1:1", 1.5: "1:1.50", 2.0: "1:2", 2.5: "1:2.50", 3.0: "1:3", 4.0: "1:4"}
    ratio_rr = st.selectbox("Ratio Riesgo/Beneficio", options=list(opciones_ratio.keys()), format_func=lambda x: opciones_ratio[x], index=3)
    
    ejecutar_btn = st.form_submit_button("Ejecutar Backtest PDH/PDL")

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
# 3. MOTOR DE BACKTESTING (PDH / PDL + FVG TOQUE)
# ==========================================
def ejecutar_backtest(df, ratio, capital_inicial, riesgo_pct):
    operaciones = []
    if df.empty: return pd.DataFrame(operaciones)
        
    df_calc = df.copy()
    fechas = df_calc['Date'].unique()
    capital_actual = capital_inicial
    
    # Empezamos desde el índice 1 porque necesitamos el día anterior (índice 0)
    for i in range(1, len(fechas)):
        fecha_actual = fechas[i]
        fecha_previa = fechas[i-1]
        
        # 1. Definir Liquidez Mayor (PDH y PDL del día anterior)
        velas_previas = df_calc[df_calc['Date'] == fecha_previa]
        pdh = velas_previas['High'].max()
        pdl = velas_previas['Low'].min()
        
        # 2. Búsqueda de Entradas en Killzone NY del día actual (08:00 a 10:30 NY)
        hora_inicio_ny = pd.to_datetime('08:00').time()
        horario_ny = df_calc.loc[(df_calc['Date'] == fecha_actual) & (df_calc.index.time >= hora_inicio_ny)]
        
        estado = "Buscando Sweep"
        tipo_trade, entrada, stop_loss = None, None, None
        entrada_limit, tp_teorico = None, None
        fvg_top, fvg_bottom = None, None
        vela_1_time = None
        
        for k in range(len(horario_ny)):
            idx = horario_ny.index[k]
            row = horario_ny.iloc[k]
            
            # Limitar búsqueda de setups nuevos hasta las 10:30 NY
            if idx.time() > pd.to_datetime('10:30').time() and entrada is None:
                break 
                
            # FASE 1: Buscar Toma de Liquidez en PDH o PDL
            if estado == "Buscando Sweep":
                if row['High'] > pdh:
                    estado = "Sweep Maximo"
                elif row['Low'] < pdl:
                    estado = "Sweep Minimo"
            
            # FASE 2: Buscar el patrón FVG de 3 velas tras barrer el PDH/PDL
            elif estado == "Sweep Maximo":
                if k >= 2:
                    c1, c2, c3 = horario_ny.iloc[k-2], horario_ny.iloc[k-1], row
                    
                    if c1['Low'] > c3['High'] and c1['Close'] > c3['Close']:
                        entrada_limit = c3['High'] 
                        stop_loss = c1['High']     
                        
                        if entrada_limit < stop_loss: 
                            tp_teorico = entrada_limit - (abs(entrada_limit - stop_loss) * ratio)
                            fvg_top, fvg_bottom = c1['Low'], c3['High']
                            vela_1_time = horario_ny.index[k-2]
                            estado = "Esperando Retroceso Short"
                            
            elif estado == "Sweep Minimo":
                if k >= 2:
                    c1, c2, c3 = horario_ny.iloc[k-2], horario_ny.iloc[k-1], row
                    
                    if c1['High'] < c3['Low'] and c1['Close'] < c3['Close']:
                        entrada_limit = c3['Low']  
                        stop_loss = c1['Low']      
                        
                        if entrada_limit > stop_loss: 
                            tp_teorico = entrada_limit + (abs(entrada_limit - stop_loss) * ratio)
                            fvg_top, fvg_bottom = c3['Low'], c1['High']
                            vela_1_time = horario_ny.index[k-2]
                            estado = "Esperando Retroceso Long"

            # FASE 3: Activar la Orden Limit al Toque
            elif estado == "Esperando Retroceso Short":
                if row['High'] >= stop_loss or row['Low'] <= tp_teorico:
                    estado = "Buscando Sweep" 
                elif row['High'] >= entrada_limit:
                    entrada, tipo_trade = entrada_limit, 'Short 🔴'
                    break 
                    
            elif estado == "Esperando Retroceso Long":
                if row['Low'] <= stop_loss or row['High'] >= tp_teorico:
                    estado = "Buscando Sweep" 
                elif row['Low'] <= entrada_limit:
                    entrada, tipo_trade = entrada_limit, 'Long 🟢'
                    break 

        # FASE 4: Gestión del Trade (hasta las 12:00)
        if entrada is not None:
            riesgo_precio = abs(entrada - stop_loss)
            if riesgo_precio == 0: riesgo_precio = 0.5
            take_profit = tp_teorico
            riesgo_usd = capital_actual * (riesgo_pct / 100)
            resultado, fecha_cierre = "Sin Resolución", None
            
            idx_entrada = horario_ny.index.get_loc(idx)
            df_post = horario_ny.iloc[idx_entrada:]
            limite_cierre = idx.replace(hour=12, minute=0, second=0)
            
            for jdx, vela in df_post.iterrows():
                # Cierre Forzado 12:00 NY
                if jdx >= limite_cierre:
                    precio_cierre = vela['Open']
                    dist = (entrada - precio_cierre) if "Short" in tipo_trade else (precio_cierre - entrada)
                    pnl_usd = (dist / riesgo_precio) * riesgo_usd
                    resultado = "Ganancia (12:00) ⏱️✅" if pnl_usd > 0 else "Pérdida (12:00) ⏱️❌"
                    fecha_cierre = jdx
                    break
                
                # Cierre SL/TP
                if ("Long" in tipo_trade and vela['Low'] <= stop_loss) or ("Short" in tipo_trade and vela['High'] >= stop_loss):
                    resultado, pnl_usd = "Pérdida (SL) ❌", -riesgo_usd
                    fecha_cierre = jdx
                    break
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
                    'PDH': pdh, 'PDL': pdl
                })

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS
# ==========================================
st.title("🎯 BOT SMC Sniper: PDH / PDL Sweep (Bitcoin)")

df_btc = obtener_datos_bingx(dias_historial)
df_operaciones = ejecutar_backtest(df_btc, ratio_rr, capital_inicial, riesgo_pct)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos.")
elif df_operaciones.empty:
    st.info("Ningún trade cumplió el patrón FVG tras barrer el PDH/PDL en el horario indicado.")
else:
    st.subheader("📊 Resumen de Rendimiento SMC PDH/PDL")
    
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
    
    st.subheader("🔍 Visualizador de Entradas Institucionales")
    opciones_trades = df_operaciones['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M').tolist()
    trade_seleccionado = st.selectbox("Selecciona un trade para visualizar el Sweep del PDH/PDL:", opciones_trades)
    
    if trade_seleccionado:
        trade = df_operaciones[df_operaciones['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M') == trade_seleccionado].iloc[0]
        dia_str = trade['Apertura (NY)'].strftime('%Y-%m-%d')
        
        # Mostramos desde las 07:00 a las 12:30 para apreciar el contexto
        df_dia = df_btc.loc[f"{dia_str} 07:00:00":f"{dia_str} 12:30:00"]
        
        fig = go.Figure(data=[go.Candlestick(
            x=df_dia.index, open=df_dia['Open'], high=df_dia['High'], low=df_dia['Low'], close=df_dia['Close'], name='BTC/USDT'
        )])
        
        # Líneas de PDH y PDL
        fig.add_hline(y=trade['PDH'], line_dash="dash", line_color="magenta", annotation_text="PDH (Max Ayer)", line_width=2)
        fig.add_hline(y=trade['PDL'], line_dash="dash", line_color="magenta", annotation_text="PDL (Min Ayer)", line_width=2)
        
        # Resaltar el FVG
        color_caja = "rgba(0, 255, 0, 0.15)" if "Long" in trade['Tipo'] else "rgba(255, 0, 0, 0.15)"
        fig.add_hrect(
            y0=trade['FVG_Bottom'], y1=trade['FVG_Top'], line_width=1, line_color="yellow", fillcolor=color_caja,
            annotation_text="FVG Limit", annotation_position="top left"
        )
        
        # Marcar la Vela 1 (Origen del Stop Loss)
        fig.add_trace(go.Scatter(
            x=[trade['Vela1_Time']], y=[trade['Stop Loss']], mode='markers', name='Vela 1 (SL)',
            marker=dict(symbol="x", size=10, color="white", line=dict(width=2, color='red'))
        ))
        
        # Punto de Entrada Exacta
        color = "#00FF00" if "Long" in trade['Tipo'] else "#FF0000"
        simbolo = "triangle-up" if "Long" in trade['Tipo'] else "triangle-down"
        
        fig.add_trace(go.Scatter(
            x=[trade['Apertura (NY)']], y=[trade['Entrada']], mode='markers', name='Toque Limit',
            marker=dict(symbol=simbolo, size=15, color=color, line=dict(width=2, color='white'))
        ))
        
        fig.add_hline(y=trade['Stop Loss'], line_dash="solid", line_color="red", annotation_text="SL")
        fig.add_hline(y=trade['Take Profit'], line_dash="solid", line_color="green", annotation_text="TP")
        
        fig.update_layout(
            title=f"Trade {trade['Tipo']} | Sweep PDH/PDL + FVG | Res: {trade['Resultado']}",
            yaxis_title="Precio Bitcoin (USD)", height=650, xaxis_rangeslider_visible=False, template="plotly_dark",
            margin=dict(l=50, r=50, t=80, b=50)
        )
        st.plotly_chart(fig, use_container_width=True)
