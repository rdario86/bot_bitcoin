import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta
import time

# Título actualizado en la pestaña del navegador
st.set_page_config(page_title="BOT Estrategia ORB (Retesteo) - Bitcoin", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA Y CAPITAL
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Ajustes ORB (Retesteo 15m)")
    
    st.subheader("💰 Gestión de Capital")
    capital_inicial = st.number_input("Bank / Capital Inicial ($)", min_value=100.0, value=1000.0, step=100.0)
    riesgo_pct = st.selectbox("Riesgo por Operación (%)", options=[1, 2, 3, 4, 5], index=2, help="Porcentaje del balance arriesgado.")
    
    st.divider()
    
    dias_historial = st.slider("Días de Backtesting", 1, 45, 30)
    
    opciones_ratio = {1.0: "1:1", 1.5: "1:1.50", 2.0: "1:2", 2.5: "1:2.50", 3.0: "1:3"}
    ratio_rr = st.selectbox(
        "Ratio Riesgo/Beneficio", 
        options=list(opciones_ratio.keys()), 
        format_func=lambda x: opciones_ratio[x],
        index=4
    )
    
    ejecutar_btn = st.form_submit_button("Confirmar y Ejecutar")

# ==========================================
# 2. CONEXIÓN A BINGX (VELAS DE 15 MINUTOS)
# ==========================================
@st.cache_data(ttl=300, show_spinner="Descargando datos de BingX Futuros (15m)...")
def obtener_datos_bingx(dias):
    try:
        exchange = ccxt.bingx({
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'}
        })
        
        ahora = pd.Timestamp.utcnow()
        inicio = ahora - pd.Timedelta(days=dias)
        since = exchange.parse8601(inicio.isoformat())
        
        todas_las_velas = []
        limite_velas = 1000 
        
        while True:
            try:
                velas = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='15m', since=since, limit=limite_velas)
                if not velas: break
                
                todas_las_velas.extend(velas)
                since = velas[-1][0] + 900000 
                
                if len(velas) < limite_velas: break 
                time.sleep(0.1) 
                
            except Exception as limite_api:
                break
                
        if not todas_las_velas:
            return pd.DataFrame()
            
        df = pd.DataFrame(todas_las_velas, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Timestamp', inplace=True)
        
        df.index = df.index.tz_localize('UTC').tz_convert('America/New_York')
        df = df[~df.index.duplicated(keep='first')]
        df['Date'] = df.index.date
        
        return df
    
    except Exception as e:
        st.error(f"Error crítico de conexión: {e}")
        return pd.DataFrame()

# ==========================================
# 3. MOTOR DE BACKTESTING (AMERICANA - RETESTEO)
# ==========================================
def ejecutar_backtest(df, ratio, capital_inicial, riesgo_pct):
    operaciones = []
    if df.empty: return pd.DataFrame(operaciones)
        
    df_calc = df.copy()
    df_calc['Tamaño_Vela'] = df_calc['High'] - df_calc['Low']
    df_calc['Promedio_Tamaño_10'] = df_calc['Tamaño_Vela'].shift(1).rolling(window=10).mean()
        
    fechas = df_calc['Date'].unique()
    capital_actual = capital_inicial
    
    for fecha in fechas:
        if fecha.weekday() >= 5: continue 
            
        vela_us = df_calc.loc[(df_calc['Date'] == fecha) & (df_calc.index.time == pd.to_datetime('09:30').time())]
        if not vela_us.empty:
            max_orb = vela_us['High'].iloc[0]
            min_orb = vela_us['Low'].iloc[0]
            
            horario_us = df_calc.loc[(df_calc['Date'] == fecha) & (df_calc.index.time >= pd.to_datetime('09:45').time()) & (df_calc.index.time <= pd.to_datetime('12:00').time())]
            
            estado_ruptura = None
            
            for idx, row in horario_us.iterrows():
                
                # 1. BÚSQUEDA DE RUPTURA INICIAL
                if estado_ruptura is None:
                    cierre = row['Close']
                    tamano = row['Tamaño_Vela']
                    prom = row['Promedio_Tamaño_10']
                    
                    if cierre > max_orb and pd.notna(prom) and tamano > prom:
                        estado_ruptura = 'Long'
                    elif cierre < min_orb and pd.notna(prom) and tamano > prom:
                        estado_ruptura = 'Short'
                
                # 2. BÚSQUEDA DE RETESTEO (Órdenes Limit)
                else:
                    entrada = None
                    tipo_trade = None
                    stop_loss = 0
                    
                    if estado_ruptura == 'Long':
                        if row['Low'] <= max_orb: # El precio hace pullback y toca el techo
                            entrada = max_orb
                            tipo_trade = 'Long 🟢'
                            stop_loss = min_orb 
                            
                    elif estado_ruptura == 'Short':
                        if row['High'] >= min_orb: # El precio hace pullback y toca el suelo
                            entrada = min_orb
                            tipo_trade = 'Short 🔴'
                            stop_loss = max_orb
                            
                    # 3. EJECUCIÓN DEL TRADE EN EL RETESTEO
                    if entrada is not None:
                        riesgo_precio = abs(entrada - stop_loss)
                        take_profit = entrada + (riesgo_precio * ratio) if "Long" in tipo_trade else entrada - (riesgo_precio * ratio)
                        
                        resultado = "Sin Resolución ⏳"
                        riesgo_usd = capital_actual * (riesgo_pct / 100)
                        df_post = df_calc.loc[idx:]
                        limite_tiempo = idx.replace(hour=16, minute=0, second=0)
                        fecha_cierre = None # <-- Variable para guardar la hora de cierre
                        
                        for jdx, vela in df_post.iterrows():
                            
                            # Cierre de 16:00
                            if jdx >= limite_tiempo:
                                precio_cierre = vela['Open']
                                dist = (precio_cierre - entrada) if "Long" in tipo_trade else (entrada - precio_cierre)
                                pnl_usd = (dist / riesgo_precio) * riesgo_usd
                                resultado = "Ganancia (16:00) ⏱️✅" if pnl_usd > 0 else "Pérdida (16:00) ⏱️❌"
                                fecha_cierre = jdx # Guardamos la hora
                                break
                                
                            # Cierre por SL/TP
                            if ("Long" in tipo_trade and vela['Low'] <= stop_loss) or ("Short" in tipo_trade and vela['High'] >= stop_loss):
                                resultado, pnl_usd = "Pérdida ❌", -riesgo_usd
                                fecha_cierre = jdx # Guardamos la hora
                                break
                            elif ("Long" in tipo_trade and vela['High'] >= take_profit) or ("Short" in tipo_trade and vela['Low'] <= take_profit):
                                resultado, pnl_usd = "Ganancia ✅", riesgo_usd * ratio
                                fecha_cierre = jdx # Guardamos la hora
                                break
                        
                        capital_actual += pnl_usd
                        operaciones.append({
                            'Apertura (NY)': idx, 
                            'Cierre (NY)': fecha_cierre, # <--- Nueva columna de cierre
                            'Tipo': tipo_trade, 'Entrada': entrada,
                            'Stop Loss': stop_loss, 'Take Profit': take_profit, 'Resultado': resultado,
                            'Max_ORB': max_orb, 'Min_ORB': min_orb,
                            'PnL ($)': pnl_usd, 'Balance': capital_actual
                        })
                        break # Termina el día tras el trade

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS
# ==========================================
st.title("📈 BOT Estrategia ORB (Entrada en Retesteo)")

df_btc = obtener_datos_bingx(dias_historial)
df_operaciones = ejecutar_backtest(df_btc, ratio_rr, capital_inicial, riesgo_pct)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos.")
elif df_operaciones.empty:
    st.info("No se encontraron operaciones en este rango de tiempo. Es posible que los rompimientos no hayan hecho retesteo.")
else:
    # División de datos
    df_regulares = df_operaciones[~df_operaciones['Resultado'].str.contains("16:00")]
    df_tiempo = df_operaciones[df_operaciones['Resultado'].str.contains("16:00")]

    total_trades = len(df_operaciones)
    balance_final = capital_inicial + df_operaciones['PnL ($)'].sum()
    ganancia_neta = df_operaciones['PnL ($)'].sum()
    rentabilidad = (ganancia_neta / capital_inicial) * 100

    st.subheader(f"📊 Resumen de Rendimiento - Breakout & Pullback")
    
    tab1, tab2, tab3 = st.tabs(["Totales (Suma)", "Cierres por SL/TP", "Cierres Forzados"])

    with tab1:
        aciertos_tot = len(df_operaciones[df_operaciones['Resultado'].str.contains("Ganancia")])
        fallos_tot = len(df_operaciones[df_operaciones['Resultado'].str.contains("Pérdida")])
        win_rate_tot = (aciertos_tot / total_trades) * 100 if total_trades > 0 else 0
        
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Operaciones", total_trades)
        c2.metric("Aciertos ✅", aciertos_tot)
        c3.metric("Fallos ❌", fallos_tot)
        c4.metric("% Win Rate Global", f"{win_rate_tot:.1f}%")
        
        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Capital Referencia", f"${capital_inicial:,.2f}")
        c6.metric("Balance Proyectado", f"${balance_final:,.2f}")
        c7.metric("PnL Neto ($)", f"${ganancia_neta:,.2f}", delta_color="normal" if ganancia_neta >= 0 else "inverse")
        c8.metric("Rentabilidad (%)", f"{rentabilidad:.2f}%")

    with tab2:
        total_reg = len(df_regulares)
        if total_reg > 0:
            aciertos_reg = len(df_regulares[df_regulares['Resultado'] == "Ganancia ✅"])
            fallos_reg = len(df_regulares[df_regulares['Resultado'] == "Pérdida ❌"])
            wr_reg = (aciertos_reg / total_reg) * 100
            pnl_reg = df_regulares['PnL ($)'].sum()
            rent_reg = (pnl_reg / capital_inicial) * 100

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Operaciones SL/TP", total_reg)
            c2.metric("Tocaron TP ✅", aciertos_reg)
            c3.metric("Tocaron SL ❌", fallos_reg)
            c4.metric("% Win Rate (Regulares)", f"{wr_reg:.1f}%")

            c5, c6, c7 = st.columns(3)
            c5.metric("PnL SL/TP ($)", f"${pnl_reg:,.2f}", delta_color="normal" if pnl_reg >= 0 else "inverse")
            c6.metric("Aporte a Rentabilidad", f"{rent_reg:.2f}%")
        else:
            st.info("No hubo operaciones cerradas por SL o TP.")

    with tab3:
        total_tmp = len(df_tiempo)
        if total_tmp > 0:
            aciertos_tmp = len(df_tiempo[df_tiempo['Resultado'].str.contains("Ganancia")])
            fallos_tmp = len(df_tiempo[df_tiempo['Resultado'].str.contains("Pérdida")])
            wr_tmp = (aciertos_tmp / total_tmp) * 100
            pnl_tmp = df_tiempo['PnL ($)'].sum()
            rent_tmp = (pnl_tmp / capital_inicial) * 100

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Cierres por Tiempo", total_tmp)
            c2.metric("En Positivo ⏱️✅", aciertos_tmp)
            c3.metric("En Negativo ⏱️❌", fallos_tmp)
            c4.metric("% Win Rate (Tiempo)", f"{wr_tmp:.1f}%")

            c5, c6, c7 = st.columns(3)
            c5.metric("PnL por Tiempo ($)", f"${pnl_tmp:,.2f}", delta_color="normal" if pnl_tmp >= 0 else "inverse")
            c6.metric("Aporte a Rentabilidad", f"{rent_tmp:.2f}%")
        else:
            st.info("Ninguna operación tuvo que ser forzada a cerrar por tiempo.")
            
    st.divider()
    
    st.subheader("📋 Registro Detallado")
    df_mostrar = df_operaciones.copy()
    df_mostrar.index = range(1, len(df_mostrar) + 1)
    
    # Formateo de fechas para que se vean legibles (YYYY-MM-DD HH:MM)
    df_mostrar['Apertura (NY)'] = df_mostrar['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M')
    df_mostrar['Cierre (NY)'] = df_mostrar['Cierre (NY)'].dt.strftime('%Y-%m-%d %H:%M')
    
    columnas_moneda = ['Entrada', 'Stop Loss', 'Take Profit', 'PnL ($)', 'Balance']
    for col in columnas_moneda:
        df_mostrar[col] = df_mostrar[col].apply(lambda x: f"-${abs(x):,.2f}" if pd.notnull(x) and x < 0 else f"${x:,.2f}" if pd.notnull(x) else x)
    
    # Aquí eliminamos la columna 'Expansión' de la lista final
    columnas_finales = ['Apertura (NY)', 'Cierre (NY)', 'Tipo', 'Entrada', 'Stop Loss', 'Take Profit', 'Resultado', 'PnL ($)', 'Balance']
    st.dataframe(df_mostrar[columnas_finales], use_container_width=True)
    
    st.divider()
    
    st.subheader("🔍 Visualizador de Operaciones (Velas de 15 Minutos)")
    opciones_trades = df_operaciones['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M').tolist()
    trade_seleccionado = st.selectbox("Selecciona la fecha de Apertura del Trade:", opciones_trades)
    
    if trade_seleccionado:
        trade_data = df_operaciones[df_operaciones['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M') == trade_seleccionado].iloc[0]
        fecha_obj = trade_data['Apertura (NY)']
        dia_str = fecha_obj.strftime('%Y-%m-%d')
        
        df_dia = df_btc.loc[f"{dia_str} 08:30:00":f"{dia_str} 16:30:00"]
        
        fig = go.Figure(data=[go.Candlestick(
            x=df_dia.index, open=df_dia['Open'], high=df_dia['High'],
            low=df_dia['Low'], close=df_dia['Close'], name='BTC/USDT'
        )])
        
        fig.add_hline(y=trade_data['Max_ORB'], line_dash="dash", line_color="blue", annotation_text="Máximo Rango 15m")
        fig.add_hline(y=trade_data['Min_ORB'], line_dash="dash", line_color="blue", annotation_text="Mínimo Rango 15m")
        
        color_flecha = "green" if "Long" in trade_data['Tipo'] else "red"
        simbolo_flecha = "triangle-up" if "Long" in trade_data['Tipo'] else "triangle-down"
        
        # El marcador se dibuja exactamente sobre la vela que tocó el nivel (el retesteo)
        fig.add_trace(go.Scatter(
            x=[fecha_obj], y=[trade_data['Entrada']], mode='markers', name='Punto de Entrada (Pullback)',
            marker=dict(symbol=simbolo_flecha, size=15, color=color_flecha)
        ))
        
        fig.add_hline(y=trade_data['Stop Loss'], line_dash="solid", line_color="red", annotation_text="Stop Loss (Estructural)")
        fig.add_hline(y=trade_data['Take Profit'], line_dash="solid", line_color="green", annotation_text="Take Profit")
        
        fig.update_layout(
            title=f"Trade {trade_data['Tipo']} (Retesteo) el {dia_str} | Resultado: {trade_data['Resultado']}",
            yaxis_title="Precio (USD)", xaxis_title="Hora (EST - NY)",
            height=600, xaxis_rangeslider_visible=False, template="plotly_dark"
        )
        
        st.plotly_chart(fig, use_container_width=True)
