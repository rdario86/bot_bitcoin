import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
import time

st.set_page_config(page_title="BOT EMAs (20/55) - Efectividad de Estrategia", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Estrategia Cruce de EMAs")
    st.markdown("""
    **Reglas de Filtrado (Color):**
    - **Long:** Cruce EMA 20 > 55. La vela debe ser **Verde**.
    - **Short:** Cruce EMA 20 < 55. La vela debe ser **Roja**.
    
    **⌚ Horarios Operativos:**
    - **Mañana (Lun-Vie):** Entradas de 08:00 a 10:30 (Cierre 11:00).
    - **Noche (Dom-Jue):** Entradas de 20:00 a 22:30 (Cierre 23:00).
    """)
    
    st.divider()
    
    st.subheader("📅 Periodo de Evaluación")
    dias_historial = st.slider("Días de Backtesting", 2, 30, 20)
    st.info("💡 Se comparará la efectividad de los Ratios 1:1, 1:2 y 1:3 simultáneamente.")
    ejecutar_btn = st.form_submit_button("Analizar Efectividad")

# ==========================================
# 2. CONEXIÓN A BINGX Y CÁLCULO DE EMAS
# ==========================================
@st.cache_data(ttl=300, show_spinner="Descargando histórico y calculando EMAs...")
def obtener_datos_bingx(dias):
    try:
        exchange = ccxt.bingx({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
        ahora = pd.Timestamp.utcnow()
        inicio = ahora - pd.Timedelta(days=dias)
        since = exchange.parse8601(inicio.isoformat())
        
        todas_las_velas = []
        limite_velas = 1000 
        intentos_fallidos = 0 
        
        while True:
            try:
                velas = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1m', since=since, limit=limite_velas)
                if not velas: 
                    break
                    
                todas_las_velas.extend(velas)
                since = velas[-1][0] + 60000 
                
                if len(velas) < limite_velas: 
                    break 
                    
                time.sleep(0.1) 
                intentos_fallidos = 0 
                
            except Exception as e:
                intentos_fallidos += 1
                if intentos_fallidos > 3: 
                    print(f"Límite de intentos alcanzado al descargar datos. Trabajando con lo obtenido hasta ahora.")
                    break
                time.sleep(1)
                
        if not todas_las_velas: 
            return pd.DataFrame()
            
        df = pd.DataFrame(todas_las_velas, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Timestamp', inplace=True)
        
        df.index = df.index.tz_localize('UTC').tz_convert('America/New_York')
        df = df[~df.index.duplicated(keep='first')]
        df['Date'] = df.index.date
        
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['EMA55'] = df['Close'].ewm(span=55, adjust=False).mean()
        
        return df
    except Exception as e:
        st.error(f"Error general en la obtención de datos: {e}")
        return pd.DataFrame()

# ==========================================
# 3. MOTOR DE BACKTESTING DE EFECTIVIDAD
# ==========================================
def ejecutar_backtest(df, ratio):
    operaciones = []
    if df.empty: return pd.DataFrame(operaciones)
        
    df_calc = df.copy()
    fechas = df_calc['Date'].unique()
    
    for fecha in fechas:
        horario_dia = df_calc.loc[df_calc['Date'] == fecha]
        
        trade_abierto = False
        tipo_trade, entrada, stop_loss, take_profit = None, None, None, None
        idx_entrada, sesion_origen = None, None 
        
        estado_espera, crossover_sl_ref, sesion_espera = None, None, None
        
        for k in range(1, len(horario_dia)):
            idx = horario_dia.index[k]
            row = horario_dia.iloc[k]
            prev_row = horario_dia.iloc[k-1]
            
            dia_semana = idx.weekday()
            es_lunes_a_viernes = dia_semana in [0, 1, 2, 3, 4]
            es_domingo_a_jueves = dia_semana in [6, 0, 1, 2, 3] 
            
            en_manana = es_lunes_a_viernes and (pd.to_datetime('08:00').time() <= idx.time() <= pd.to_datetime('10:30').time())
            en_noche = es_domingo_a_jueves and (pd.to_datetime('20:00').time() <= idx.time() <= pd.to_datetime('22:30').time())
            
            sesion_actual = "manana" if en_manana else ("noche" if en_noche else None)
            
            # --- 1. EVALUACIÓN Y BÚSQUEDA DE ENTRADAS ---
            if not trade_abierto:
                pre_entrada, pre_sl, pre_tipo, origen = None, None, None, None
                
                # A. Evaluación de Espera
                if estado_espera == "Esperando_Long" and row['Close'] >= row['Open']:
                    pre_entrada, pre_sl, pre_tipo, origen = row['Close'], min(crossover_sl_ref, row['Low']), 'Long 🟢 (Conf)', sesion_espera
                    estado_espera = None 
                elif estado_espera == "Esperando_Short" and row['Close'] <= row['Open']:
                    pre_entrada, pre_sl, pre_tipo, origen = row['Close'], max(crossover_sl_ref, row['High']), 'Short 🔴 (Conf)', sesion_espera
                    estado_espera = None
                    
                # B. Búsqueda de Cruces Nuevos
                elif estado_espera is None and sesion_actual is not None:
                    cruce_alcista = (prev_row['EMA20'] <= prev_row['EMA55']) and (row['EMA20'] > row['EMA55'])
                    cruce_bajista = (prev_row['EMA20'] >= prev_row['EMA55']) and (row['EMA20'] < row['EMA55'])
                    
                    if cruce_alcista:
                        if row['Close'] >= row['Open']:
                            pre_entrada, pre_sl, pre_tipo, origen = row['Close'], row['Low'], 'Long 🟢', sesion_actual
                        else:
                            estado_espera, crossover_sl_ref, sesion_espera = "Esperando_Long", row['Low'], sesion_actual
                            
                    elif cruce_bajista:
                        if row['Close'] <= row['Open']:
                            pre_entrada, pre_sl, pre_tipo, origen = row['Close'], row['High'], 'Short 🔴', sesion_actual
                        else:
                            estado_espera, crossover_sl_ref, sesion_espera = "Esperando_Short", row['High'], sesion_actual
                            
                # C. Ejecutar Cálculos de Entrada
                if pre_entrada is not None:
                    riesgo_precio = abs(pre_entrada - pre_sl)
                    if riesgo_precio == 0:
                        margen = pre_entrada * 0.0005
                        riesgo_precio = margen
                        pre_sl = (pre_entrada - margen) if "Long" in pre_tipo else (pre_entrada + margen)
                        
                    # Set variables globales del trade
                    trade_abierto = True
                    tipo_trade, entrada, stop_loss = pre_tipo, pre_entrada, pre_sl
                    take_profit = (entrada + (riesgo_precio * ratio)) if "Long" in tipo_trade else (entrada - (riesgo_precio * ratio))
                    idx_entrada, sesion_origen = idx, origen

            # --- 2. GESTIÓN DEL TRADE Y CIERRES ---
            elif trade_abierto:
                resultado = None
                fecha_cierre = idx
                es_cierre_forzado = False
                lbl_cierre = ""
                
                if sesion_origen == "manana" and idx.time() >= pd.to_datetime('11:00').time():
                    es_cierre_forzado = True
                    lbl_cierre = "11:00"
                elif sesion_origen == "noche" and idx.time() >= pd.to_datetime('23:00').time():
                    es_cierre_forzado = True
                    lbl_cierre = "23:00"
                
                # 1. Cierre Forzado
                if es_cierre_forzado:
                    precio_salida = row['Open']
                    ganador = (precio_salida > entrada) if "Long" in tipo_trade else (precio_salida < entrada)
                    resultado = f"Ganancia ({lbl_cierre}) ⏱️✅" if ganador else f"Pérdida ({lbl_cierre}) ⏱️❌"
                    
                # 2. Toca Stop Loss
                elif ("Long" in tipo_trade and row['Low'] <= stop_loss) or ("Short" in tipo_trade and row['High'] >= stop_loss):
                    resultado = "Pérdida (SL) ❌"
                    
                # 3. Toca Take Profit
                elif ("Long" in tipo_trade and row['High'] >= take_profit) or ("Short" in tipo_trade and row['Low'] <= take_profit):
                    resultado = "Ganancia (TP) ✅"
                
                if resultado is not None:
                    operaciones.append({
                        'Apertura (NY)': idx_entrada, 'Cierre (NY)': fecha_cierre, 'Tipo': tipo_trade,
                        'Entrada': entrada, 'Stop Loss': stop_loss, 'Take Profit': take_profit,
                        'Resultado': resultado
                    })
                    trade_abierto = False 
                    estado_espera = None

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y COMPARATIVA DE RATIOS
# ==========================================
st.title("📈 Análisis de Efectividad: Cruce de EMAs")

df_btc = obtener_datos_bingx(dias_historial)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos de BingX.")
else:
    df_rr1 = ejecutar_backtest(df_btc, 1.0)
    df_rr2 = ejecutar_backtest(df_btc, 2.0)
    df_rr3 = ejecutar_backtest(df_btc, 3.0)

    st.subheader("🎯 Comparativa de Win Rate por Ratio (R/R)")
    
    col1, col2, col3 = st.columns(3)
    
    def generar_metricas(df, ratio_str, col):
        if df.empty:
            col.info(f"Ratio {ratio_str}: Sin trades")
            return
            
        total = len(df)
        aciertos = len(df[df['Resultado'].str.contains("Ganancia")])
        fallos = total - aciertos
        win_rate = (aciertos / total) * 100 if total > 0 else 0
        
        with col:
            st.markdown(f"### Ratio {ratio_str}")
            st.metric("Win Rate", f"{win_rate:.1f}%")
            st.write(f"**Total Trades:** {total}")
            st.write(f"**✅ Aciertos:** {aciertos} | **❌ Fallos:** {fallos}")

    generar_metricas(df_rr1, "1:1", col1)
    generar_metricas(df_rr2, "1:2", col2)
    generar_metricas(df_rr3, "1:3", col3)
    
    st.divider()

    st.subheader("🔍 Registro de Operaciones")
    ratio_seleccionado = st.radio("Selecciona el Ratio para ver su registro:", ["Ratio 1:1", "Ratio 1:2", "Ratio 1:3"], horizontal=True)
    
    df_mostrar = df_rr1 if "1:1" in ratio_seleccionado else (df_rr2 if "1:2" in ratio_seleccionado else df_rr3)
    
    if df_mostrar.empty:
        st.info("No hay trades en este periodo.")
    else:
        df_tabla = df_mostrar.copy()
        df_tabla.index = range(1, len(df_tabla) + 1)
        df_mostrar_fechas_reales = df_tabla.copy()
        
        df_tabla['Apertura (NY)'] = df_tabla['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M')
        df_tabla['Cierre (NY)'] = df_tabla['Cierre (NY)'].dt.strftime('%Y-%m-%d %H:%M')
        for col in ['Entrada', 'Stop Loss', 'Take Profit']:
            df_tabla[col] = df_tabla[col].apply(lambda x: f"${x:,.2f}")
            
        st.dataframe(df_tabla[['Apertura (NY)', 'Cierre (NY)', 'Tipo', 'Entrada', 'Stop Loss', 'Take Profit', 'Resultado']], use_container_width=True)
        
        st.write("### 📊 Gráfica de Entradas y EMAs")
        opciones_trades = [f"{row['Apertura (NY)']} | {row['Tipo']} | Res: {row['Resultado']}" for _, row in df_tabla.iterrows()]
        trade_str = st.selectbox("Selecciona un trade para ver el gráfico:", opciones_trades)
        
        if trade_str:
            fecha_str = trade_str.split(" | ")[0]
            dia_str = fecha_str.split(" ")[0]
            hora_apertura = int(fecha_str.split(" ")[1].split(":")[0])
            
            trade = df_mostrar_fechas_reales[df_tabla['Apertura (NY)'] == fecha_str].iloc[0]
            
            if hora_apertura >= 19:
                df_dia = df_btc.loc[f"{dia_str} 19:30:00":f"{dia_str} 23:30:00"]
            else:
                df_dia = df_btc.loc[f"{dia_str} 07:30:00":f"{dia_str} 11:30:00"]
            
            fig = go.Figure()
            
            fig.add_trace(go.Candlestick(
                x=df_dia.index, open=df_dia['Open'], high=df_dia['High'], low=df_dia['Low'], close=df_dia['Close'], name='BTC/USDT'
            ))
            
            fig.add_trace(go.Scatter(x=df_dia.index, y=df_dia['EMA20'], mode='lines', name='EMA 20', line=dict(color='#00BFFF', width=2)))
            fig.add_trace(go.Scatter(x=df_dia.index, y=df_dia['EMA55'], mode='lines', name='EMA 55', line=dict(color='#FFA500', width=2)))
            
            color = "#00FF00" if "Long" in trade['Tipo'] else "#FF0000"
            simbolo = "triangle-up" if "Long" in trade['Tipo'] else "triangle-down"
            
            fig.add_trace(go.Scatter(
                x=[trade['Apertura (NY)']], y=[trade['Entrada']], mode='markers', name='Cruce (Entrada)',
                marker=dict(symbol=simbolo, size=18, color=color, line=dict(width=2, color='white'))
            ))
            
            fig.add_hline(y=trade['Stop Loss'], line_dash="solid", line_color="red", annotation_text="Stop Loss")
            fig.add_hline(y=trade['Take Profit'], line_dash="solid", line_color="green", annotation_text="Take Profit")
            
            fig.update_layout(
                title=f"Trade {trade['Tipo']} | Resultado: {trade['Resultado']}",
                yaxis_title="Precio Bitcoin", height=650, xaxis_rangeslider_visible=False, template="plotly_dark",
                margin=dict(l=50, r=50, t=80, b=50)
            )
            st.plotly_chart(fig, use_container_width=True)
