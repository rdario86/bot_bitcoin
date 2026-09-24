import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta
import time

st.set_page_config(page_title="BOT SMC PRO: Doble Barrido - BTC", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ SMC: Doble Barrido")
    st.markdown("""
    **Lógica de Liquidez:**
    - Si Londres tomó el PDL ➡️ NY espera barrer el **PDH** (Para entrar Short).
    - Si Londres tomó el PDH ➡️ NY espera barrer el **PDL** (Para entrar Long).
    - Si Londres fue neutral ➡️ NY espera que se barra cualquiera de los dos.
    """)
    
    st.subheader("💰 Gestión de Capital")
    capital_inicial = st.number_input("Capital Inicial ($)", min_value=100.0, value=1000.0, step=100.0)
    riesgo_pct = st.selectbox("Riesgo por Operación (%)", options=[1, 2, 3, 4, 5], index=1)
    
    st.divider()
    
    dias_historial = st.slider("Días de Backtesting", 2, 30, 20)
    
    opciones_ratio = {1.0: "1:1", 1.5: "1:1.50", 2.0: "1:2", 2.5: "1:2.50", 3.0: "1:3", 4.0: "1:4"}
    ratio_rr = st.selectbox("Ratio Riesgo/Beneficio", options=list(opciones_ratio.keys()), format_func=lambda x: opciones_ratio[x], index=3)
    
    ejecutar_btn = st.form_submit_button("Ejecutar Algoritmo")

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
# 3. MOTOR SMC (FILTRO POR NARRATIVA)
# ==========================================
def ejecutar_backtest(df, ratio, capital_inicial, riesgo_pct):
    operaciones = []
    if df.empty: return pd.DataFrame(operaciones)
        
    df_calc = df.copy()
    fechas = df_calc['Date'].unique()
    capital_actual = capital_inicial
    
    for i in range(1, len(fechas)):
        fecha_actual = fechas[i]
        fecha_previa = fechas[i-1]
        
        # 1. Definir Liquidez Mayor (PDH y PDL de ayer)
        velas_previas = df_calc[df_calc['Date'] == fecha_previa]
        pdh = velas_previas['High'].max()
        pdl = velas_previas['Low'].min()
        
        # 2. Evaluar qué hizo Londres (00:00 a 08:00)
        londres = df_calc.loc[(df_calc['Date'] == fecha_actual) & (df_calc.index.time < pd.to_datetime('08:00').time())]
        londres_max = londres['High'].max() if not londres.empty else 0
        londres_min = londres['Low'].min() if not londres.empty else 9999999
        
        narrativa = "Neutral"
        if londres_min < pdl and londres_max > pdh:
            narrativa = "Neutral" # Si por algún milagro tomó ambos, reseteamos a neutral
        elif londres_min < pdl:
            narrativa = "Esperar PDH" # Ya sacó a los compradores, ahora vamos por los vendedores
        elif londres_max > pdh:
            narrativa = "Esperar PDL" # Ya sacó a los vendedores, ahora vamos por los compradores
            
        # 3. Búsqueda de Entradas NY (08:00 a 10:30)
        hora_inicio_ny = pd.to_datetime('08:00').time()
        horario_ny = df_calc.loc[(df_calc['Date'] == fecha_actual) & (df_calc.index.time >= hora_inicio_ny)]
        
        estado = "Buscando Sweep"
        tipo_trade, entrada, stop_loss = None, None, None
        entrada_limit, tp_teorico = None, None
        fvg_top, fvg_bottom, vela_1_time = None, None, None
        
        for k in range(len(horario_ny)):
            idx = horario_ny.index[k]
            row = horario_ny.iloc[k]
            
            if idx.time() > pd.to_datetime('10:30').time() and entrada is None:
                break 
                
            # FASE 1: Buscar Toma de Liquidez filtrada por la Narrativa
            if estado == "Buscando Sweep":
                if narrativa == "Esperar PDH":
                    # El bot ignora el PDL, solo espera que el precio suba y barra el PDH
                    if row['High'] > pdh: estado = "Sweep Maximo"
                
                elif narrativa == "Esperar PDL":
                    # El bot ignora el PDH, solo espera que el precio baje y barra el PDL
                    if row['Low'] < pdl: estado = "Sweep Minimo"
                
                elif narrativa == "Neutral":
                    # Como no pasó nada en Londres, esperamos que rompa cualquiera de los dos
                    if row['High'] > pdh: estado = "Sweep Maximo"
                    elif row['Low'] < pdl: estado = "Sweep Minimo"
            
            # FASE 2: Patrón FVG de 3 velas tras barrer la liquidez correcta
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
                
                # Cierre SL/TP Intrabarra
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
                dia_string = fecha_actual.strftime('%Y-%m-%d')
                operaciones.append({
                    'Día': dia_string,
                    'Regla NY': narrativa,
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
st.title("🎯 BOT SMC PRO: Doble Barrido Inteligente")

df_btc = obtener_datos_bingx(dias_historial)
df_operaciones = ejecutar_backtest(df_btc, ratio_rr, capital_inicial, riesgo_pct)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos de BingX.")
elif df_operaciones.empty:
    st.info("Ningún trade cumplió con los parámetros institucionales estrictos para este periodo.")
else:
    st.subheader("📊 Resumen de Rendimiento SMC Inteligente")
    
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
    
    st.subheader("🔍 Visualizador (Contexto de Liquidez 24h)")
    # Muestra la instrucción que seguía el bot para ese día específico
    opciones_trades = [f"{row['Apertura (NY)'].strftime('%Y-%m-%d %H:%M')} | Misión: {row['Regla NY']} | {row['Tipo']}" for _, row in df_operaciones.iterrows()]
    trade_seleccionado = st.selectbox("Selecciona un trade para ver el comportamiento del doble barrido:", opciones_trades)
    
    if trade_seleccionado:
        fecha_str = trade_seleccionado.split(" | ")[0]
        trade = df_operaciones[df_operaciones['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M') == fecha_str].iloc[0]
        dia_str = trade['Día']
        
        # Mostramos la gráfica desde las 00:00 para ver la acción de Londres
        df_dia = df_btc.loc[f"{dia_str} 00:00:00":f"{dia_str} 12:30:00"]
        
        fig = go.Figure(data=[go.Candlestick(
            x=df_dia.index, open=df_dia['Open'], high=df_dia['High'], low=df_dia['Low'], close=df_dia['Close'], name='BTC/USDT'
        )])
        
        # Sombreado de la sesión de Londres (Para ver el primer barrido)
        fig.add_vrect(
            x0=f"{dia_str} 00:00:00", x1=f"{dia_str} 08:00:00",
            fillcolor="white", opacity=0.03, line_width=0, annotation_text="Londres (Primer Barrido)", annotation_position="top left"
        )
        
        # Líneas de Liquidez Mayor
        fig.add_hline(y=trade['PDH'], line_dash="solid", line_color="magenta", annotation_text="PDH (Liquidez Superior)", line_width=2)
        fig.add_hline(y=trade['PDL'], line_dash="solid", line_color="magenta", annotation_text="PDL (Liquidez Inferior)", line_width=2)
        
        # Resaltar el FVG de Entrada
        color_caja = "rgba(0, 255, 0, 0.15)" if "Long" in trade['Tipo'] else "rgba(255, 0, 0, 0.15)"
        fig.add_hrect(
            y0=trade['FVG_Bottom'], y1=trade['FVG_Top'], line_width=1, line_color="yellow", fillcolor=color_caja,
            annotation_text="FVG Limit", annotation_position="top left"
        )
        
        # Origen Vela 1 (Stop Loss)
        fig.add_trace(go.Scatter(
            x=[trade['Vela1_Time']], y=[trade['Stop Loss']], mode='markers', name='Vela 1 (SL)',
            marker=dict(symbol="x", size=10, color="white", line=dict(width=2, color='red'))
        ))
        
        # Punto de Entrada Exacta
        color = "#00FF00" if "Long" in trade['Tipo'] else "#FF0000"
        simbolo = "triangle-up" if "Long" in trade['Tipo'] else "triangle-down"
        
        fig.add_trace(go.Scatter(
            x=[trade['Apertura (NY)']], y=[trade['Entrada']], mode='markers', name='Entrada NY',
            marker=dict(symbol=simbolo, size=15, color=color, line=dict(width=2, color='white'))
        ))
        
        fig.add_hline(y=trade['Take Profit'], line_dash="solid", line_color="green", annotation_text="Take Profit")
        
        fig.update_layout(
            title=f"Día: {trade['Regla NY']} | {trade['Tipo']} | Res: {trade['Resultado']}",
            yaxis_title="Precio Bitcoin (USD)", height=650, xaxis_rangeslider_visible=False, template="plotly_dark",
            margin=dict(l=50, r=50, t=80, b=50)
        )
        st.plotly_chart(fig, use_container_width=True)
