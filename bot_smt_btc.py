import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta
import time

st.set_page_config(page_title="BOT Estrategia EMAs (20/55) - Bitcoin", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA Y CAPITAL
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Ajustes Estrategia EMAs")
    st.markdown("""
    **Estrategia Tendencial:**
    - EMA 20 y EMA 55.
    - Entrada al cierre de la vela que produce el cruce.
    - Stop Loss: Mínimo/Máximo de la vela de cruce.
    - Horario: 08:00 a 12:00 NY.
    - Cierre Forzado: 16:00 NY.
    """)
    
    st.subheader("💰 Gestión de Capital")
    capital_inicial = st.number_input("Bank / Capital Inicial ($)", min_value=100.0, value=1000.0, step=100.0)
    riesgo_pct = st.selectbox("Riesgo por Operación (%)", options=[1, 2, 3, 4, 5], index=2, help="Porcentaje del balance arriesgado.")
    
    st.divider()
    
    dias_historial = st.slider("Días de Backtesting", 1, 30, 15, help="Limitado a un máximo de 30 días por API.")
    
    st.info("Se ejecutarán y compararán los ratios 1:1, 1:2 y 1:3 simultáneamente.")
    
    ejecutar_btn = st.form_submit_button("Confirmar y Ejecutar Backtest BTC")

# ==========================================
# 2. CONEXIÓN A BINGX (VELAS DE 1 MINUTO)
# ==========================================
@st.cache_data(ttl=300, show_spinner="Descargando histórico de Bitcoin (BingX 1m)...")
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
                
            except ccxt.NetworkError as e:
                st.warning("Problema de red, reintentando...")
                time.sleep(1)
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
        
        # Calcular EMAs
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['EMA55'] = df['Close'].ewm(span=55, adjust=False).mean()
        
        return df
    
    except Exception as e:
        st.error(f"Error crítico de conexión: {e}")
        return pd.DataFrame()

# ==========================================
# 3. MOTOR DE BACKTESTING (ESTRATEGIA EMAS)
# ==========================================
def ejecutar_backtest(df, ratio, capital_inicial, riesgo_pct):
    operaciones = []
    if df.empty: return pd.DataFrame(operaciones)
        
    df_calc = df.copy()
    fechas = df_calc['Date'].unique()
    capital_actual = capital_inicial
    
    for fecha in fechas:
        if fecha.weekday() >= 5: continue 
            
        hora_inicio = pd.to_datetime('08:00').time()
        hora_fin = pd.to_datetime('12:00').time()
        
        horario_operativo = df_calc.loc[(df_calc['Date'] == fecha) & (df_calc.index.time >= hora_inicio) & (df_calc.index.time <= hora_fin)]
        
        trade_abierto = False
        tipo_trade, entrada, stop_loss, take_profit = None, None, None, None
        idx_entrada = None
        
        for k in range(1, len(horario_operativo)):
            idx = horario_operativo.index[k]
            row = horario_operativo.iloc[k]
            prev_row = horario_operativo.iloc[k-1]
            
            # Buscar Cruces
            if not trade_abierto:
                # Cruce Alcista (Long)
                if prev_row['EMA20'] <= prev_row['EMA55'] and row['EMA20'] > row['EMA55']:
                    trade_abierto = True
                    tipo_trade = 'Long 🟢'
                    entrada = row['Close']
                    stop_loss = row['Low'] # Stop en el mínimo de la vela
                    if stop_loss >= entrada: stop_loss = entrada * 0.999
                    riesgo_precio = entrada - stop_loss
                    take_profit = entrada + (riesgo_precio * ratio)
                    idx_entrada = idx
                
                # Cruce Bajista (Short)
                elif prev_row['EMA20'] >= prev_row['EMA55'] and row['EMA20'] < row['EMA55']:
                    trade_abierto = True
                    tipo_trade = 'Short 🔴'
                    entrada = row['Close']
                    stop_loss = row['High'] # Stop en el máximo de la vela
                    if stop_loss <= entrada: stop_loss = entrada * 1.001
                    riesgo_precio = stop_loss - entrada
                    take_profit = entrada - (riesgo_precio * ratio)
                    idx_entrada = idx

            # Gestionar Trade Abierto
            elif trade_abierto:
                resultado = None
                riesgo_usd = capital_actual * (riesgo_pct / 100)
                fecha_cierre = idx
                
                # Cierre Forzado a las 16:00
                if idx.time() >= pd.to_datetime('16:00').time():
                    precio_cierre = row['Open']
                    dist = (precio_cierre - entrada) if "Long" in tipo_trade else (entrada - precio_cierre)
                    pnl_usd = (dist / riesgo_precio) * riesgo_usd
                    resultado = "Ganancia (16:00) ⏱️✅" if pnl_usd > 0 else "Pérdida (16:00) ⏱️❌"
                
                # Stop Loss
                elif ("Long" in tipo_trade and row['Low'] <= stop_loss) or ("Short" in tipo_trade and row['High'] >= stop_loss):
                    resultado, pnl_usd = "Pérdida (SL) ❌", -riesgo_usd
                    
                # Take Profit
                elif ("Long" in tipo_trade and row['High'] >= take_profit) or ("Short" in tipo_trade and row['Low'] <= take_profit):
                    resultado, pnl_usd = "Ganancia (TP) ✅", riesgo_usd * ratio

                if resultado is not None:
                    capital_actual += pnl_usd
                    operaciones.append({
                        'Apertura (NY)': idx_entrada, 
                        'Cierre (NY)': fecha_cierre, 
                        'Tipo': tipo_trade, 'Entrada': entrada,
                        'Stop Loss': stop_loss, 'Take Profit': take_profit, 'Resultado': resultado,
                        'PnL ($)': pnl_usd, 'Balance': capital_actual
                    })
                    trade_abierto = False

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS
# ==========================================
st.title("📈 BOT Estrategia EMAs (20/55) - Bitcoin")
st.write("Análisis de cruce de medias móviles en velas de 1 minuto (08:00 - 12:00 NY)")

df_btc = obtener_datos_bingx(dias_historial)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos de la red de BingX. Intenta nuevamente.")
else:
    # Ejecutar backtests
    df_rr1 = ejecutar_backtest(df_btc, 1.0, capital_inicial, riesgo_pct)
    df_rr2 = ejecutar_backtest(df_btc, 2.0, capital_inicial, riesgo_pct)
    df_rr3 = ejecutar_backtest(df_btc, 3.0, capital_inicial, riesgo_pct)

    st.subheader("⚖️ Comparativa de Rentabilidad por Ratio (R/R)")
    
    col1, col2, col3 = st.columns(3)
    
    def generar_metricas(df, ratio_str, col):
        if df.empty:
            col.info(f"Ratio {ratio_str}: Sin operaciones")
            return
            
        total = len(df)
        aciertos = len(df[df['Resultado'].str.contains("Ganancia")])
        win_rate = (aciertos / total) * 100
        neto = df['PnL ($)'].sum()
        
        with col:
            st.markdown(f"### Ratio {ratio_str}")
            st.metric("Win Rate", f"{win_rate:.1f}%", f"{aciertos} aciertos de {total}")
            st.metric("Beneficio Neto", f"${neto:,.2f}", delta_color="normal" if neto >= 0 else "inverse")
            st.metric("Balance Final", f"${capital_inicial + neto:,.2f}")

    generar_metricas(df_rr1, "1:1", col1)
    generar_metricas(df_rr2, "1:2", col2)
    generar_metricas(df_rr3, "1:3", col3)

    st.divider()
    
    st.subheader("📋 Registro Detallado y Visualización")
    ratio_seleccionado = st.radio("Selecciona el Ratio para ver los detalles:", ["Ratio 1:1", "Ratio 1:2", "Ratio 1:3"], horizontal=True)
    
    df_mostrar = df_rr1 if "1:1" in ratio_seleccionado else (df_rr2 if "1:2" in ratio_seleccionado else df_rr3)
    
    if df_mostrar.empty:
        st.info("No hay trades registrados.")
    else:
        df_tabla = df_mostrar.copy()
        df_tabla.index = range(1, len(df_tabla) + 1)
        df_tabla['Apertura (NY)'] = df_tabla['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M')
        df_tabla['Cierre (NY)'] = df_tabla['Cierre (NY)'].dt.strftime('%Y-%m-%d %H:%M')
        
        columnas_moneda = ['Entrada', 'Stop Loss', 'Take Profit', 'PnL ($)', 'Balance']
        for col in columnas_moneda:
            df_tabla[col] = df_tabla[col].apply(lambda x: f"-${abs(x):,.2f}" if pd.notnull(x) and x < 0 else f"${x:,.2f}" if pd.notnull(x) else x)
            
        columnas_finales = ['Apertura (NY)', 'Cierre (NY)', 'Tipo', 'Entrada', 'Stop Loss', 'Take Profit', 'Resultado', 'PnL ($)', 'Balance']
        st.dataframe(df_tabla[columnas_finales], use_container_width=True)
        
        st.divider()
        
        st.subheader("🔍 Visualizador de Acción del Precio y EMAs")
        opciones_trades = [f"{row['Apertura (NY)'].strftime('%Y-%m-%d %H:%M')} | {row['Tipo']}" for _, row in df_mostrar.iterrows()]
        trade_seleccionado = st.selectbox("Selecciona la fecha del Trade:", opciones_trades)
        
        if trade_seleccionado:
            fecha_str = trade_seleccionado.split(" | ")[0]
            trade_data = df_mostrar[df_mostrar['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M') == fecha_str].iloc[0]
            fecha_obj = trade_data['Apertura (NY)']
            dia_str = fecha_obj.strftime('%Y-%m-%d')
            
            # Ampliar la ventana gráfica para ver bien las EMAs
            hora_ini_grafico = (fecha_obj - pd.Timedelta(hours=1)).strftime('%H:%M:%S')
            hora_fin_grafico = (trade_data['Cierre (NY)'] + pd.Timedelta(hours=1)).strftime('%H:%M:%S')
            df_dia = df_btc.loc[f"{dia_str} {hora_ini_grafico}":f"{dia_str} {hora_fin_grafico}"]
            
            fig = go.Figure(data=[go.Candlestick(
                x=df_dia.index, open=df_dia['Open'], high=df_dia['High'],
                low=df_dia['Low'], close=df_dia['Close'], name='BTC/USDT (1m)'
            )])
            
            # Añadir EMAs
            fig.add_trace(go.Scatter(x=df_dia.index, y=df_dia['EMA20'], mode='lines', line=dict(color='cyan', width=1.5), name='EMA 20'))
            fig.add_trace(go.Scatter(x=df_dia.index, y=df_dia['EMA55'], mode='lines', line=dict(color='orange', width=1.5), name='EMA 55'))
            
            color_flecha = "#00FF00" if "Long" in trade_data['Tipo'] else "#FF0000"
            simbolo_flecha = "triangle-up" if "Long" in trade_data['Tipo'] else "triangle-down"
            
            fig.add_trace(go.Scatter(
                x=[fecha_obj], y=[trade_data['Entrada']], mode='markers', name='Punto de Entrada',
                marker=dict(symbol=simbolo_flecha, size=15, color=color_flecha, line=dict(width=2, color='white'))
            ))
            
            fig.add_hline(y=trade_data['Stop Loss'], line_dash="solid", line_color="red", annotation_text="Stop Loss (Vela)")
            fig.add_hline(y=trade_data['Take Profit'], line_dash="solid", line_color="green", annotation_text="Take Profit")
            
            fig.update_layout(
                title=f"Trade {trade_data['Tipo']} (Cruce EMAs) el {dia_str} | Resultado: {trade_data['Resultado']}",
                yaxis_title="Precio (USD)", xaxis_title="Hora (EST - NY)",
                height=650, xaxis_rangeslider_visible=False, template="plotly_dark",
                margin=dict(l=50, r=50, t=80, b=50)
            )
            
            st.plotly_chart(fig, use_container_width=True)
    
