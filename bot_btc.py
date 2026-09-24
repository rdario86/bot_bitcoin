import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta
import time

st.set_page_config(page_title="BOT Estrategia ORB (11:00 NY)", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA Y CAPITAL
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Ajustes Estrategia (11:00 NY)")
    st.markdown("""
    **Estrategia:**
    1. Rango 15m (11:00 - 11:14).
    2. Ruptura con cuerpo en vela de 5m.
    3. Entrada Limit en el Retesteo.
    4. Cierre 16:00 NY.
    """)
    
    st.subheader("💰 Gestión de Capital")
    capital_inicial = st.number_input("Bank / Capital Inicial ($)", min_value=100.0, value=1000.0, step=100.0)
    riesgo_pct = st.selectbox("Riesgo por Operación (%)", options=[1, 2, 3, 4, 5], index=2, help="Porcentaje del balance arriesgado.")
    
    st.divider()
    
    dias_historial = st.slider("Días de Backtesting", 1, 30, 15, help="Limitado a un máximo de 30 días.")
    
    opciones_ratio = {1.0: "1:1", 1.5: "1:1.50", 2.0: "1:2", 2.5: "1:2.50", 3.0: "1:3"}
    ratio_rr = st.selectbox(
        "Ratio Riesgo/Beneficio", 
        options=list(opciones_ratio.keys()), 
        format_func=lambda x: opciones_ratio[x],
        index=3
    )
    
    ejecutar_btn = st.form_submit_button("Confirmar y Ejecutar")

# ==========================================
# 2. CONEXIÓN A BINGX (VELAS DE 1 MINUTO)
# ==========================================
@st.cache_data(ttl=300, show_spinner="Descargando histórico de BingX Futuros...")
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
                velas = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1m', since=since, limit=limite_velas)
                if not velas: break
                
                todas_las_velas.extend(velas)
                since = velas[-1][0] + 60000 
                
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
# 3. MOTOR DE BACKTESTING (NUEVA LÓGICA 11:00 NY)
# ==========================================
def ejecutar_backtest(df, ratio, capital_inicial, riesgo_pct):
    operaciones = []
    if df.empty: return pd.DataFrame(operaciones)
        
    df_calc = df.copy()
    fechas = df_calc['Date'].unique()
    capital_actual = capital_inicial
    
    for fecha in fechas:
        if fecha.weekday() >= 5: continue # Omitir fines de semana
            
        # 1. Fijar el Rango de la vela de 15 minutos (11:00 a 11:14:59)
        hora_inicio_rango = pd.to_datetime('11:00').time()
        hora_fin_rango = pd.to_datetime('11:14').time()
        
        velas_rango = df_calc.loc[(df_calc['Date'] == fecha) & (df_calc.index.time >= hora_inicio_rango) & (df_calc.index.time <= hora_fin_rango)]
        
        if len(velas_rango) >= 15: # Validar que tenemos los datos completos de los 15 min
            max_rango = velas_rango['High'].max()
            min_rango = velas_rango['Low'].min()
            
            # Ventana operativa: Desde las 11:15 hasta las 15:59
            horario_us = df_calc.loc[(df_calc['Date'] == fecha) & (df_calc.index.time >= pd.to_datetime('11:15').time()) & (df_calc.index.time < pd.to_datetime('16:00').time())]
            
            estado_ruptura = None
            tiempo_ruptura = None
            
            for idx, row in horario_us.iterrows():
                
                # FASE 1: BÚSQUEDA DE RUPTURA CON CUERPO EN VELA DE 5 MINUTOS
                if estado_ruptura is None:
                    # Matemáticamente, la vela de 5m cierra en los minutos terminados en 4 o 9 (ej. 11:19, 11:24)
                    if row.name.minute % 5 == 4:
                        cierre_5m = row['Close']
                        
                        if cierre_5m > max_rango:
                            estado_ruptura = 'Long'
                            tiempo_ruptura = row.name
                        elif cierre_5m < min_rango:
                            estado_ruptura = 'Short'
                            tiempo_ruptura = row.name
                
                # FASE 2: PULLBACK (RETESTEO) Y ENTRADA
                else:
                    entrada = None
                    tipo_trade = None
                    stop_loss = 0
                    
                    if row.name > tiempo_ruptura:
                        if estado_ruptura == 'Long':
                            if row['Low'] <= max_rango:
                                entrada = max_rango # Entramos EXACTO en la línea del rango
                                tipo_trade = 'Long 🟢'
                                stop_loss = min_rango # El stop es el otro extremo
                                
                        elif estado_ruptura == 'Short':
                            if row['High'] >= min_rango:
                                entrada = min_rango # Entramos EXACTO en la línea del rango
                                tipo_trade = 'Short 🔴'
                                stop_loss = max_rango # El stop es el otro extremo
                            
                    # EJECUCIÓN DEL TRADE
                    if entrada is not None:
                        riesgo_precio = abs(entrada - stop_loss)
                        if riesgo_precio == 0: riesgo_precio = 0.01 
                        
                        porcentaje_sl = (riesgo_precio / entrada) * 100
                        take_profit = entrada + (riesgo_precio * ratio) if "Long" in tipo_trade else entrada - (riesgo_precio * ratio)
                        
                        resultado = "Sin Resolución ⏳"
                        riesgo_usd = capital_actual * (riesgo_pct / 100)
                        
                        df_post = df_calc.loc[idx:]
                        
                        # CIERRE AUTOMÁTICO A LAS 16:00 NY
                        limite_tiempo = idx.replace(hour=16, minute=0, second=0)
                        fecha_cierre = None 
                        
                        for jdx, vela in df_post.iterrows():
                            
                            # Guillotina por tiempo a las 16:00
                            if jdx >= limite_tiempo:
                                precio_cierre = vela['Open'] 
                                dist = (precio_cierre - entrada) if "Long" in tipo_trade else (entrada - precio_cierre)
                                pnl_usd = (dist / riesgo_precio) * riesgo_usd
                                resultado = "Ganancia (16:00) ⏱️✅" if pnl_usd > 0 else "Pérdida (16:00) ⏱️❌"
                                fecha_cierre = jdx 
                                break
                                
                            # Cierre por alcanzar Stop Loss o Take Profit
                            if ("Long" in tipo_trade and vela['Low'] <= stop_loss) or ("Short" in tipo_trade and vela['High'] >= stop_loss):
                                resultado, pnl_usd = "Pérdida ❌", -riesgo_usd
                                fecha_cierre = jdx 
                                break
                            elif ("Long" in tipo_trade and vela['High'] >= take_profit) or ("Short" in tipo_trade and vela['Low'] <= take_profit):
                                resultado, pnl_usd = "Ganancia ✅", riesgo_usd * ratio
                                fecha_cierre = jdx 
                                break
                        
                        capital_actual += pnl_usd
                        operaciones.append({
                            'Apertura (NY)': idx, 
                            'Cierre (NY)': fecha_cierre, 
                            'Tipo': tipo_trade, 'Entrada': entrada,
                            'Stop Loss': stop_loss, 'Tamaño SL (%)': porcentaje_sl, 'Take Profit': take_profit, 'Resultado': resultado,
                            'Max_ORB': max_rango, 'Min_ORB': min_rango,
                            'PnL ($)': pnl_usd, 'Balance': capital_actual
                        })
                        break # Solo 1 trade al día

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS
# ==========================================
st.title("📈 BOT Estrategia Rango 11:00 NY (Break & Retest)")

df_btc = obtener_datos_bingx(dias_historial)
df_operaciones = ejecutar_backtest(df_btc, ratio_rr, capital_inicial, riesgo_pct)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos.")
elif df_operaciones.empty:
    st.info("No se encontraron operaciones en este rango de tiempo que cumplan con la ruptura de 5m y posterior retesteo antes de las 16:00.")
else:
    df_regulares = df_operaciones[~df_operaciones['Resultado'].str.contains("16:00")]
    df_tiempo = df_operaciones[df_operaciones['Resultado'].str.contains("16:00")]

    total_trades = len(df_operaciones)
    balance_final = capital_inicial + df_operaciones['PnL ($)'].sum()
    ganancia_neta = df_operaciones['PnL ($)'].sum()
    rentabilidad = (ganancia_neta / capital_inicial) * 100

    st.subheader(f"📊 Resumen de Rendimiento")
    
    tab1, tab2, tab3 = st.tabs(["Totales (Suma)", "Cierres por SL/TP", "Cierres Forzados (16:00)"])

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
            st.info("Ninguna operación tuvo que ser forzada a cerrar por tiempo a las 16:00.")
            
    st.divider()
    
    st.subheader("📋 Registro Detallado")
    df_mostrar = df_operaciones.copy()
    df_mostrar.index = range(1, len(df_mostrar) + 1)
    
    df_mostrar['Apertura (NY)'] = df_mostrar['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M')
    df_mostrar['Cierre (NY)'] = df_mostrar['Cierre (NY)'].dt.strftime('%Y-%m-%d %H:%M')
    
    columnas_moneda = ['Entrada', 'Stop Loss', 'Take Profit', 'PnL ($)', 'Balance']
    for col in columnas_moneda:
        df_mostrar[col] = df_mostrar[col].apply(lambda x: f"-${abs(x):,.2f}" if pd.notnull(x) and x < 0 else f"${x:,.2f}" if pd.notnull(x) else x)
        
    df_mostrar['Tamaño SL (%)'] = df_mostrar['Tamaño SL (%)'].apply(lambda x: f"{x:.2f}%")
    
    columnas_finales = ['Apertura (NY)', 'Cierre (NY)', 'Tipo', 'Entrada', 'Stop Loss', 'Tamaño SL (%)', 'Take Profit', 'Resultado', 'PnL ($)', 'Balance']
    st.dataframe(df_mostrar[columnas_finales], use_container_width=True)
    
    st.divider()
    
    st.subheader("🔍 Visualizador de Operaciones (Gráfico Interactivo)")
    opciones_trades = df_operaciones['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M').tolist()
    trade_seleccionado = st.selectbox("Selecciona la fecha de Confirmación del Trade:", opciones_trades)
    
    if trade_seleccionado:
        trade_data = df_operaciones[df_operaciones['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M') == trade_seleccionado].iloc[0]
        fecha_obj = trade_data['Apertura (NY)']
        dia_str = fecha_obj.strftime('%Y-%m-%d')
        
        # Ajustamos el gráfico para ver toda la sesión de la tarde (10:30 a 16:30)
        df_dia = df_btc.loc[f"{dia_str} 10:30:00":f"{dia_str} 16:30:00"]
        
        fig = go.Figure(data=[go.Candlestick(
            x=df_dia.index, open=df_dia['Open'], high=df_dia['High'],
            low=df_dia['Low'], close=df_dia['Close'], name='BTC/USDT (1m)'
        )])
        
        fig.add_hline(y=trade_data['Max_ORB'], line_dash="dash", line_color="blue", annotation_text="Techo (Rango 11:00)")
        fig.add_hline(y=trade_data['Min_ORB'], line_dash="dash", line_color="blue", annotation_text="Suelo (Rango 11:00)")
        
        color_flecha = "green" if "Long" in trade_data['Tipo'] else "red"
        simbolo_flecha = "triangle-up" if "Long" in trade_data['Tipo'] else "triangle-down"
        
        fig.add_trace(go.Scatter(
            x=[fecha_obj], y=[trade_data['Entrada']], mode='markers', name='Punto de Entrada (Retesteo)',
            marker=dict(symbol=simbolo_flecha, size=15, color=color_flecha)
        ))
        
        fig.add_hline(y=trade_data['Stop Loss'], line_dash="solid", line_color="red", annotation_text="Stop Loss")
        fig.add_hline(y=trade_data['Take Profit'], line_dash="solid", line_color="green", annotation_text="Take Profit")
        
        fig.update_layout(
            title=f"Trade {trade_data['Tipo']} el {dia_str} | Resultado: {trade_data['Resultado']}",
            yaxis_title="Precio (USD)", xaxis_title="Hora (EST - NY)",
            height=600, xaxis_rangeslider_visible=False, template="plotly_dark"
        )
        
        st.plotly_chart(fig, use_container_width=True)
