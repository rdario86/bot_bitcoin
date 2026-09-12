import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta
import time

# Título actualizado en la pestaña del navegador
st.set_page_config(page_title="BOT Estrategia ORB - Bitcoin", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA Y CAPITAL
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Ajustes ORB (15 Min)")
    
    # Nuevos parámetros de gestión de capital
    st.subheader("💰 Gestión de Capital")
    capital_inicial = st.number_input("Bank / Capital Inicial ($)", min_value=100.0, value=1000.0, step=100.0)
    riesgo_pct = st.selectbox("Riesgo por Operación (%)", options=[1, 2, 3, 4, 5], index=0, help="Porcentaje del balance actual que se arriesgará en caso de tocar el Stop Loss.")
    
    st.divider()
    
    dias_historial = st.slider("Días de Backtesting", 1, 45, 30)
    ratio_rr = st.number_input("Ratio Riesgo/Beneficio (1:X)", min_value=0.1, value=2.0, step=0.1)
    
    ejecutar_btn = st.form_submit_button("Confirmar Ajustes y Ejecutar")

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
                if not velas:
                    break
                
                todas_las_velas.extend(velas)
                since = velas[-1][0] + 900000 
                
                if len(velas) < limite_velas:
                    break 
                
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
# 3. MOTOR DE BACKTESTING CON GESTIÓN DE CAPITAL
# ==========================================
def ejecutar_backtest(df, ratio, capital_inicial, riesgo_pct):
    operaciones = []
    if df.empty:
        return pd.DataFrame(operaciones)
        
    df_calc = df.copy()
    df_calc['Tamaño_Vela'] = df_calc['High'] - df_calc['Low']
    
    periodos_x = 10
    df_calc['Promedio_Tamaño_10'] = df_calc['Tamaño_Vela'].shift(1).rolling(window=periodos_x).mean()
        
    fechas = df_calc['Date'].unique()
    hora_cierre_tiempo = pd.to_datetime('16:00').time()
    
    capital_actual = capital_inicial
    
    for fecha in fechas:
        df_dia = df_calc[df_calc['Date'] == fecha]
        
        vela_apertura = df_dia.between_time('09:30', '09:30')
        if vela_apertura.empty:
            continue
            
        max_orb = vela_apertura['High'].iloc[0]
        min_orb = vela_apertura['Low'].iloc[0]
        
        horario_operativo = df_dia.between_time('09:45', '12:00')
        
        for idx, row in horario_operativo.iterrows():
            entrada = row['Close']
            tamaño_actual = row['Tamaño_Vela']
            promedio_anterior = row['Promedio_Tamaño_10']
            tipo_trade = None
            
            if entrada > max_orb:
                if pd.notna(promedio_anterior):
                    if tamaño_actual <= promedio_anterior:
                        continue 
                        
                tipo_trade = 'Long 🟢'
                stop_loss = min_orb 
                riesgo_precio = entrada - stop_loss
                take_profit = entrada + (riesgo_precio * ratio)
                    
            elif entrada < min_orb:
                if pd.notna(promedio_anterior):
                    if tamaño_actual <= promedio_anterior:
                        continue 
                        
                tipo_trade = 'Short 🔴'
                stop_loss = max_orb 
                riesgo_precio = stop_loss - entrada
                take_profit = entrada - (riesgo_precio * ratio)
                
            if tipo_trade:
                resultado = "Sin Resolución ⏳"
                pnl_usd = 0.0
                riesgo_usd = capital_actual * (riesgo_pct / 100)
                
                df_post_entrada = df_dia.loc[idx:]
                
                for jdx, vela in df_post_entrada.iterrows():
                    if jdx == idx: continue 
                    
                    # 1. Validación de Cierre por Tiempo (16:00 NY)
                    if jdx.time() >= hora_cierre_tiempo:
                        precio_cierre = vela['Open']
                        if "Long" in tipo_trade:
                            distancia_recorrida = precio_cierre - entrada
                        else:
                            distancia_recorrida = entrada - precio_cierre
                            
                        # Calcular PnL proporcional al avance hasta las 16:00
                        pnl_usd = (distancia_recorrida / riesgo_precio) * riesgo_usd
                        
                        if pnl_usd > 0:
                            resultado = "Ganancia (16:00) ⏱️✅"
                        else:
                            resultado = "Pérdida (16:00) ⏱️❌"
                        break
                    
                    # 2. Validación normal de TP / SL
                    if "Long" in tipo_trade:
                        if vela['Low'] <= stop_loss:
                            resultado = "Pérdida ❌"
                            pnl_usd = -riesgo_usd
                            break
                        elif vela['High'] >= take_profit:
                            resultado = "Ganancia ✅"
                            pnl_usd = riesgo_usd * ratio
                            break
                    else: 
                        if vela['High'] >= stop_loss:
                            resultado = "Pérdida ❌"
                            pnl_usd = -riesgo_usd
                            break
                        elif vela['Low'] <= take_profit:
                            resultado = "Ganancia ✅"
                            pnl_usd = riesgo_usd * ratio
                            break
                
                # Actualizar el capital de la cuenta con el resultado del trade
                capital_actual += pnl_usd
                
                operaciones.append({
                    'Fecha': idx, 'Tipo': tipo_trade, 'Entrada': entrada,
                    'Stop Loss': stop_loss, 'Take Profit': take_profit,
                    'Resultado': resultado,
                    'Max_ORB': max_orb, 'Min_ORB': min_orb,
                    'Vela_Ruptura': tamaño_actual, 'Promedio_10_Velas': promedio_anterior,
                    'PnL ($)': pnl_usd, 'Balance': capital_actual
                })
                
                break 

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS
# ==========================================
st.title("📈 BOT Estrategia ORB - Bitcoin")

df_btc = obtener_datos_bingx(dias_historial)
df_operaciones = ejecutar_backtest(df_btc, ratio_rr, capital_inicial, riesgo_pct)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos.")
elif df_operaciones.empty:
    st.info("No se encontraron operaciones. El filtro de expansión de vela (10 periodos) podría estar bloqueando entradas con bajo momentum.")
else:
    total_trades = len(df_operaciones)
    aciertos = len(df_operaciones[df_operaciones['Resultado'].str.contains("Ganancia")])
    fallos = len(df_operaciones[df_operaciones['Resultado'].str.contains("Pérdida")])
    win_rate = (aciertos / total_trades) * 100 if total_trades > 0 else 0
    
    balance_final = df_operaciones['Balance'].iloc[-1]
    ganancia_neta = balance_final - capital_inicial
    rentabilidad = (ganancia_neta / capital_inicial) * 100
    
    st.subheader("📊 Resumen de Rendimiento y Rentabilidad")
    
    # Primera fila de métricas (Estadísticas del sistema)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Operaciones", total_trades)
    col2.metric("Aciertos ✅", aciertos)
    col3.metric("Fallos ❌", fallos)
    col4.metric("% Win Rate", f"{win_rate:.1f}%")
    
    # Segunda fila de métricas (Dinero)
    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Capital Inicial", f"${capital_inicial:,.2f}")
    col6.metric("Balance Final", f"${balance_final:,.2f}", delta=f"${ganancia_neta:,.2f}")
    col7.metric("Ganancia/Pérdida Neta ($)", f"${ganancia_neta:,.2f}", delta_color="normal" if ganancia_neta >= 0 else "inverse")
    col8.metric("Rentabilidad (%)", f"{rentabilidad:.2f}%")
    
    st.divider()
    
    st.subheader("📋 Registro Detallado")
    df_mostrar = df_operaciones.copy()
    
    df_mostrar.index = range(1, len(df_mostrar) + 1)
    
    columnas_moneda = ['Entrada', 'Stop Loss', 'Take Profit', 'PnL ($)', 'Balance']
    for col in columnas_moneda:
        df_mostrar[col] = df_mostrar[col].apply(lambda x: f"${x:,.2f}")
        
    df_mostrar['Expansión'] = (df_mostrar['Vela_Ruptura'] / df_mostrar['Promedio_10_Velas']).apply(lambda x: f"{x:.2f}x el prom.")
    
    # Reordenar las columnas para una mejor lectura financiera
    columnas_finales = ['Fecha', 'Tipo', 'Entrada', 'Stop Loss', 'Take Profit', 'Expansión', 'Resultado', 'PnL ($)', 'Balance']
    st.dataframe(df_mostrar[columnas_finales], use_container_width=True)
    
    st.divider()
    
    st.subheader("🔍 Visualizador de Operaciones (Velas de 15 Minutos)")
    opciones_trades = df_operaciones['Fecha'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist()
    trade_seleccionado = st.selectbox("Selecciona la fecha del Trade:", opciones_trades)
    
    if trade_seleccionado:
        trade_data = df_operaciones[df_operaciones['Fecha'].dt.strftime('%Y-%m-%d %H:%M:%S') == trade_seleccionado].iloc[0]
        fecha_obj = trade_data['Fecha']
        dia_str = fecha_obj.strftime('%Y-%m-%d')
        
        df_dia = df_btc.loc[f"{dia_str} 08:30:00":f"{dia_str} 16:30:00"]
        
        fig = go.Figure(data=[go.Candlestick(
            x=df_dia.index,
            open=df_dia['Open'], high=df_dia['High'],
            low=df_dia['Low'], close=df_dia['Close'],
            name='BTC/USDT'
        )])
        
        fig.add_hline(y=trade_data['Max_ORB'], line_dash="dash", line_color="blue", annotation_text="Máximo Rango 15m")
        fig.add_hline(y=trade_data['Min_ORB'], line_dash="dash", line_color="blue", annotation_text="Mínimo Rango 15m")
        
        color_flecha = "green" if "Long" in trade_data['Tipo'] else "red"
        simbolo_flecha = "triangle-up" if "Long" in trade_data['Tipo'] else "triangle-down"
        
        fig.add_trace(go.Scatter(
            x=[fecha_obj], y=[trade_data['Entrada']],
            mode='markers', name='Punto de Entrada',
            marker=dict(symbol=simbolo_flecha, size=15, color=color_flecha)
        ))
        
        fig.add_hline(y=trade_data['Stop Loss'], line_dash="solid", line_color="red", annotation_text="Stop Loss (Estructural)")
        fig.add_hline(y=trade_data['Take Profit'], line_dash="solid", line_color="green", annotation_text="Take Profit")
        
        fig.update_layout(
            title=f"Trade {trade_data['Tipo']} el {dia_str} | Resultado: {trade_data['Resultado']}",
            yaxis_title="Precio (USD)",
            xaxis_title="Hora (EST - NY)",
            height=600,
            xaxis_rangeslider_visible=False,
            template="plotly_dark"
        )
        
        st.plotly_chart(fig, use_container_width=True)
