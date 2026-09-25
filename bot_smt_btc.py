import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
import time

st.set_page_config(page_title="BOT EMAs (20 y 55) - Bitcoin", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Estrategia Cruce de EMAs")
    st.markdown("""
    **Reglas del Video:**
    - **Indicadores:** EMA 20 y EMA 55.
    - **Señal:** Cruce de EMAs.
    - **Horario Entradas:** 08:00 a 12:00 NY.
    - **Cierre Forzado:** 16:00 NY.
    - **Stop Loss:** Último swing (15 velas).
    """)
    
    st.subheader("💰 Gestión de Capital")
    capital_inicial = st.number_input("Capital Inicial ($)", min_value=100.0, value=1000.0, step=100.0)
    riesgo_pct = st.selectbox("Riesgo por Operación (%)", options=[1, 2, 3, 4, 5], index=1)
    
    st.divider()
    
    dias_historial = st.slider("Días de Backtesting", 2, 30, 20)
    
    st.info("💡 El sistema ejecutará automáticamente el backtest para los Ratios 1:1, 1:2 y 1:3 en paralelo para compararlos.")
    
    ejecutar_btn = st.form_submit_button("Ejecutar Backtest Múltiple")

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
        
        # Calcular las EMAs (20 y 55)
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['EMA55'] = df['Close'].ewm(span=55, adjust=False).mean()
        
        return df
    except Exception as e:
        st.error(f"Error de conexión: {e}")
        return pd.DataFrame()

# ==========================================
# 3. MOTOR DE BACKTESTING (CRUCE EMAS)
# ==========================================
def ejecutar_backtest(df, ratio, capital_inicial, riesgo_pct):
    operaciones = []
    if df.empty: return pd.DataFrame(operaciones)
        
    df_calc = df.copy()
    fechas = df_calc['Date'].unique()
    capital_actual = capital_inicial
    
    for fecha in fechas:
        # Rango operativo
        hora_inicio = pd.to_datetime('08:00').time()
        hora_fin = pd.to_datetime('12:00').time()
        
        horario_dia = df_calc.loc[(df_calc['Date'] == fecha) & (df_calc.index.time >= hora_inicio)]
        
        trade_abierto = False
        tipo_trade, entrada, stop_loss, take_profit = None, None, None, None
        idx_entrada = None
        
        for k in range(1, len(horario_dia)):
            idx = horario_dia.index[k]
            row = horario_dia.iloc[k]
            prev_row = horario_dia.iloc[k-1]
            
            # 1. BUSCAR CRUCE (Solo de 08:00 a 12:00 y si no hay trade abierto)
            if not trade_abierto and idx.time() <= hora_fin:
                
                # Cruce Alcista (EMA 20 cruza arriba de EMA 55)
                if prev_row['EMA20'] <= prev_row['EMA55'] and row['EMA20'] > row['EMA55']:
                    trade_abierto = True
                    tipo_trade = 'Long 🟢'
                    entrada = row['Close'] # Se entra al cierre de la vela que cruza
                    
                    # Stop Loss: Mínimo del swing que generó el cruce (últimas 15 velas)
                    idx_global = df_calc.index.get_loc(idx)
                    velas_previas = df_calc.iloc[max(0, idx_global-15):idx_global]
                    stop_loss = velas_previas['Low'].min()
                    
                    if stop_loss >= entrada: stop_loss = entrada * 0.999 # Protección anti-errores
                        
                    riesgo = entrada - stop_loss
                    take_profit = entrada + (riesgo * ratio)
                    idx_entrada = idx
                    
                # Cruce Bajista (EMA 20 cruza abajo de EMA 55)
                elif prev_row['EMA20'] >= prev_row['EMA55'] and row['EMA20'] < row['EMA55']:
                    trade_abierto = True
                    tipo_trade = 'Short 🔴'
                    entrada = row['Close']
                    
                    # Stop Loss: Máximo del swing (últimas 15 velas)
                    idx_global = df_calc.index.get_loc(idx)
                    velas_previas = df_calc.iloc[max(0, idx_global-15):idx_global]
                    stop_loss = velas_previas['High'].max()
                    
                    if stop_loss <= entrada: stop_loss = entrada * 1.001 
                        
                    riesgo = stop_loss - entrada
                    take_profit = entrada - (riesgo * ratio)
                    idx_entrada = idx

            # 2. GESTIÓN DEL TRADE (Si hay posición abierta)
            elif trade_abierto:
                resultado = None
                fecha_cierre = idx
                riesgo_usd = capital_actual * (riesgo_pct / 100)
                
                # A. Cierre Forzado a las 16:00 NY
                if idx.time() >= pd.to_datetime('16:00').time():
                    precio_cierre = row['Open']
                    dist = (precio_cierre - entrada) if "Long" in tipo_trade else (entrada - precio_cierre)
                    riesgo_precio = abs(entrada - stop_loss)
                    pnl_usd = (dist / riesgo_precio) * riesgo_usd
                    resultado = "Ganancia (16:00) ⏱️✅" if pnl_usd > 0 else "Pérdida (16:00) ⏱️❌"
                    
                # B. Toca Stop Loss
                elif ("Long" in tipo_trade and row['Low'] <= stop_loss) or ("Short" in tipo_trade and row['High'] >= stop_loss):
                    pnl_usd = -riesgo_usd
                    resultado = "Pérdida (SL) ❌"
                    
                # C. Toca Take Profit
                elif ("Long" in tipo_trade and row['High'] >= take_profit) or ("Short" in tipo_trade and row['Low'] <= take_profit):
                    pnl_usd = riesgo_usd * ratio
                    resultado = "Ganancia (TP) ✅"
                
                # Registro y reinicio para permitir múltiples trades al día
                if resultado is not None:
                    capital_actual += pnl_usd
                    operaciones.append({
                        'Apertura (NY)': idx_entrada, 'Cierre (NY)': fecha_cierre, 'Tipo': tipo_trade,
                        'Entrada': entrada, 'Stop Loss': stop_loss, 'Take Profit': take_profit,
                        'Resultado': resultado, 'PnL ($)': pnl_usd, 'Balance': capital_actual
                    })
                    trade_abierto = False 

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y COMPARATIVA DE RATIOS
# ==========================================
st.title("📈 BOT Tendencia: Cruce de EMAs (20 y 55)")

df_btc = obtener_datos_bingx(dias_historial)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos de BingX.")
else:
    # Correr el backtest para los 3 ratios en simultáneo
    df_rr1 = ejecutar_backtest(df_btc, 1.0, capital_inicial, riesgo_pct)
    df_rr2 = ejecutar_backtest(df_btc, 2.0, capital_inicial, riesgo_pct)
    df_rr3 = ejecutar_backtest(df_btc, 3.0, capital_inicial, riesgo_pct)

    st.subheader("⚖️ Comparativa de Rentabilidad por Ratio (R/R)")
    
    col1, col2, col3 = st.columns(3)
    
    def generar_metricas(df, ratio_str, col):
        if df.empty:
            col.info(f"Ratio {ratio_str}: Sin trades")
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

    # Selector para ver la tabla y la gráfica del Ratio deseado
    st.subheader("🔍 Visualizador de Operaciones")
    ratio_seleccionado = st.radio("Selecciona el Ratio para ver el detalle de sus operaciones:", ["Ratio 1:1", "Ratio 1:2", "Ratio 1:3"], horizontal=True)
    
    df_mostrar = df_rr1 if "1:1" in ratio_seleccionado else (df_rr2 if "1:2" in ratio_seleccionado else df_rr3)
    
    if df_mostrar.empty:
        st.info("No hay trades en este periodo.")
    else:
        # Tabla detallada
        df_tabla = df_mostrar.copy()
        df_tabla.index = range(1, len(df_tabla) + 1)
        df_tabla['Apertura (NY)'] = df_tabla['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M')
        df_tabla['Cierre (NY)'] = df_tabla['Cierre (NY)'].dt.strftime('%Y-%m-%d %H:%M')
        for col in ['Entrada', 'Stop Loss', 'Take Profit', 'PnL ($)', 'Balance']:
            df_tabla[col] = df_tabla[col].apply(lambda x: f"${x:,.2f}")
            
        st.dataframe(df_tabla[['Apertura (NY)', 'Cierre (NY)', 'Tipo', 'Entrada', 'Stop Loss', 'Take Profit', 'Resultado', 'PnL ($)', 'Balance']], use_container_width=True)
        
        # Gráfica Plotly
        st.write("### 📊 Gráfica de Entradas y EMAs")
        opciones_trades = [f"{row['Apertura (NY)'].strftime('%Y-%m-%d %H:%M')} | {row['Tipo']}" for _, row in df_mostrar.iterrows()]
        trade_str = st.selectbox("Selecciona un trade para ver el Cruce:", opciones_trades)
        
        if trade_str:
            fecha_str = trade_str.split(" | ")[0]
            trade = df_mostrar[df_mostrar['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M') == fecha_str].iloc[0]
            dia_str = trade['Apertura (NY)'].strftime('%Y-%m-%d')
            
            # Gráfica de 07:30 a 16:30
            df_dia = df_btc.loc[f"{dia_str} 07:30:00":f"{dia_str} 16:30:00"]
            
            fig = go.Figure()
            
            # Velas
            fig.add_trace(go.Candlestick(
                x=df_dia.index, open=df_dia['Open'], high=df_dia['High'], low=df_dia['Low'], close=df_dia['Close'], name='BTC/USDT'
            ))
            
            # EMAs
            fig.add_trace(go.Scatter(x=df_dia.index, y=df_dia['EMA20'], mode='lines', name='EMA 20', line=dict(color='#00BFFF', width=2)))
            fig.add_trace(go.Scatter(x=df_dia.index, y=df_dia['EMA55'], mode='lines', name='EMA 55', line=dict(color='#FFA500', width=2)))
            
            # Punto de Entrada
            color = "#00FF00" if "Long" in trade['Tipo'] else "#FF0000"
            simbolo = "triangle-up" if "Long" in trade['Tipo'] else "triangle-down"
            
            fig.add_trace(go.Scatter(
                x=[trade['Apertura (NY)']], y=[trade['Entrada']], mode='markers', name='Cruce (Entrada)',
                marker=dict(symbol=simbolo, size=18, color=color, line=dict(width=2, color='white'))
            ))
            
            fig.add_hline(y=trade['Stop Loss'], line_dash="solid", line_color="red", annotation_text="Stop Loss (Último Swing)")
            fig.add_hline(y=trade['Take Profit'], line_dash="solid", line_color="green", annotation_text="Take Profit")
            
            fig.update_layout(
                title=f"Trade {trade['Tipo']} | Res: {trade['Resultado']}",
                yaxis_title="Precio Bitcoin", height=650, xaxis_rangeslider_visible=False, template="plotly_dark",
                margin=dict(l=50, r=50, t=80, b=50)
            )
            st.plotly_chart(fig, use_container_width=True)