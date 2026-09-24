import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta
import time

st.set_page_config(page_title="BOT Estrategia Smart Money (Liquidez + ChoCh) - Bitcoin", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA Y CAPITAL
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Ajustes Smart Money BTC")
    st.markdown("""
    **Estrategia (Liquidez + ChoCh):**
    1. Define el Máx/Mín de las sesiones previas (Asia/Londres).
    2. Espera la toma de liquidez (Sweep) en la sesión NY.
    3. Espera un Cambio de Estructura (ChoCh) en 1m.
    4. Entra en dirección contraria a la toma de liquidez.
    """)
    
    st.subheader("💰 Gestión de Capital")
    capital_inicial = st.number_input("Bank / Capital Inicial ($)", min_value=100.0, value=1000.0, step=100.0)
    riesgo_pct = st.selectbox("Riesgo por Operación (%)", options=[1, 2, 3, 4, 5], index=1, help="Porcentaje del balance arriesgado.")
    
    st.divider()
    
    dias_historial = st.slider("Días de Backtesting", 1, 30, 15, help="Histórico en velas de 1 minuto.")
    
    opciones_ratio = {1.0: "1:1", 1.5: "1:1.50", 2.0: "1:2", 2.5: "1:2.50", 3.0: "1:3", 4.0: "1:4"}
    ratio_rr = st.selectbox(
        "Ratio Riesgo/Beneficio", 
        options=list(opciones_ratio.keys()), 
        format_func=lambda x: opciones_ratio[x],
        index=2 # Ratio 1:2 por defecto como sugiere el video
    )
    
    ejecutar_btn = st.form_submit_button("Ejecutar Backtest SMC BTC")

# ==========================================
# 2. CONEXIÓN A BINGX (VELAS DE 1 MINUTO)
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
                
            except ccxt.NetworkError:
                time.sleep(1)
            except Exception:
                break
                
        if not todas_las_velas: return pd.DataFrame()
            
        df = pd.DataFrame(todas_las_velas, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Timestamp', inplace=True)
        
        # Ajuste a la franja horaria de Nueva York
        df.index = df.index.tz_localize('UTC').tz_convert('America/New_York')
        df = df[~df.index.duplicated(keep='first')]
        df['Date'] = df.index.date
        
        return df
    
    except Exception as e:
        st.error(f"Error de conexión: {e}")
        return pd.DataFrame()

# ==========================================
# 3. MOTOR DE BACKTESTING (LIQUIDEZ + CHOCH)
# ==========================================
def ejecutar_backtest(df, ratio, capital_inicial, riesgo_pct):
    operaciones = []
    if df.empty: return pd.DataFrame(operaciones)
        
    df_calc = df.copy()
    fechas = df_calc['Date'].unique()
    capital_actual = capital_inicial
    
    for fecha in fechas:
        # 1. Definir la Liquidez Previa (Sesión Asiática/Londres: 00:00 a 08:00 NY)
        hora_inicio_liq = pd.to_datetime('00:00').time()
        hora_fin_liq = pd.to_datetime('08:00').time()
        
        velas_liq = df_calc.loc[(df_calc['Date'] == fecha) & (df_calc.index.time >= hora_inicio_liq) & (df_calc.index.time < hora_fin_liq)]
        
        if len(velas_liq) < 60: continue # Evitar días con datos incompletos
            
        liq_max = velas_liq['High'].max()
        liq_min = velas_liq['Low'].min()
        
        # 2. Operativa en Sesión NY (08:00 a 12:00 NY)
        horario_ny = df_calc.loc[(df_calc['Date'] == fecha) & (df_calc.index.time >= hora_fin_liq) & (df_calc.index.time <= pd.to_datetime('12:00').time())]
        
        estado = "Buscando Sweep"
        tipo_trade = None
        entrada, stop_loss = None, None
        sweep_peak = None
        choch_level = None
        
        for idx, row in horario_ny.iterrows():
            
            # FASE 1: Esperar que el precio tome liquidez (Sweep)
            if estado == "Buscando Sweep":
                if row['High'] > liq_max:
                    estado = "Sweep Maximo"
                    sweep_peak = row['High']
                    # El ChoCh estará en el mínimo más bajo reciente antes de la toma
                    velas_previas = horario_ny.loc[:idx].tail(10)
                    choch_level = velas_previas['Low'].min()
                
                elif row['Low'] < liq_min:
                    estado = "Sweep Minimo"
                    sweep_peak = row['Low']
                    # El ChoCh estará en el máximo más alto reciente antes de la toma
                    velas_previas = horario_ny.loc[:idx].tail(10)
                    choch_level = velas_previas['High'].max()
            
            # FASE 2: Barrido detectado, actualizar el pico y buscar Cambio de Estructura (ChoCh)
            elif estado == "Sweep Maximo":
                sweep_peak = max(sweep_peak, row['High'])
                
                # Confirmación de ChoCh: El precio cierra por debajo del último mínimo válido
                if row['Close'] < choch_level:
                    entrada = row['Close']
                    stop_loss = sweep_peak
                    tipo_trade = 'Short 🔴'
                    break
                    
            elif estado == "Sweep Minimo":
                sweep_peak = min(sweep_peak, row['Low'])
                
                # Confirmación de ChoCh: El precio cierra por encima del último máximo válido
                if row['Close'] > choch_level:
                    entrada = row['Close']
                    stop_loss = sweep_peak
                    tipo_trade = 'Long 🟢'
                    break

        # FASE 3: Si se confirmó una entrada, gestionar el SL y TP
        if entrada is not None and stop_loss != entrada:
            riesgo_precio = abs(entrada - stop_loss)
            if riesgo_precio == 0: riesgo_precio = 0.5
            
            take_profit = entrada - (riesgo_precio * ratio) if "Short" in tipo_trade else entrada + (riesgo_precio * ratio)
            
            riesgo_usd = capital_actual * (riesgo_pct / 100)
            resultado = "Sin Resolución ⏳"
            fecha_cierre = None
            
            df_post = df_calc.loc[idx + pd.Timedelta(minutes=1):]
            
            for jdx, vela in df_post.iterrows():
                # Cierre al final de la sesión si no toca nada (Evitar over-holding)
                if jdx.time() >= pd.to_datetime('16:00').time():
                    precio_cierre = vela['Open']
                    dist = (entrada - precio_cierre) if "Short" in tipo_trade else (precio_cierre - entrada)
                    pnl_usd = (dist / riesgo_precio) * riesgo_usd
                    resultado = "Cierre por Sesión ⏱️"
                    fecha_cierre = jdx
                    break
                
                # Toca SL
                if ("Long" in tipo_trade and vela['Low'] <= stop_loss) or ("Short" in tipo_trade and vela['High'] >= stop_loss):
                    resultado, pnl_usd = "Pérdida (SL) ❌", -riesgo_usd
                    fecha_cierre = jdx
                    break
                
                # Toca TP
                elif ("Long" in tipo_trade and vela['High'] >= take_profit) or ("Short" in tipo_trade and vela['Low'] <= take_profit):
                    resultado, pnl_usd = "Ganancia (TP) ✅", riesgo_usd * ratio
                    fecha_cierre = jdx
                    break
            
            if fecha_cierre is not None:
                capital_actual += pnl_usd
                operaciones.append({
                    'Apertura (NY)': idx, 
                    'Cierre (NY)': fecha_cierre, 
                    'Tipo': tipo_trade, 'Entrada': entrada,
                    'Stop Loss': stop_loss, 'Take Profit': take_profit, 'Resultado': resultado,
                    'Liq_Max': liq_max, 'Liq_Min': liq_min,
                    'PnL ($)': pnl_usd, 'Balance': capital_actual
                })

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS
# ==========================================
st.title("📉 BOT SMC: Barrido de Liquidez + ChoCh (Bitcoin)")
st.write("Detecta zonas de liquidez dejadas en Londres/Asia y opera la manipulación institucional en la sesión de Nueva York.")

df_btc = obtener_datos_bingx(dias_historial)
df_operaciones = ejecutar_backtest(df_btc, ratio_rr, capital_inicial, riesgo_pct)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos.")
elif df_operaciones.empty:
    st.info("No se encontraron operaciones en este periodo que cumplan con la toma de liquidez y confirmación de ChoCh.")
else:
    total_trades = len(df_operaciones)
    balance_final = capital_inicial + df_operaciones['PnL ($)'].sum()
    ganancia_neta = df_operaciones['PnL ($)'].sum()
    
    aciertos = len(df_operaciones[df_operaciones['Resultado'].str.contains("Ganancia")])
    fallos = len(df_operaciones[df_operaciones['Resultado'].str.contains("Pérdida")])
    win_rate = (aciertos / total_trades) * 100

    st.subheader(f"📊 Resumen de Rendimiento SMC (Ratio {ratio_rr}:1)")
    
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Trades SMC", total_trades)
    c2.metric("Aciertos (TP) ✅", aciertos)
    c3.metric("Fallos (SL) ❌", fallos)
    c4.metric("% Win Rate", f"{win_rate:.1f}%")
    
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Capital Inicial", f"${capital_inicial:,.2f}")
    c6.metric("Balance Proyectado", f"${balance_final:,.2f}")
    c7.metric("PnL Neto ($)", f"${ganancia_neta:,.2f}", delta_color="normal" if ganancia_neta >= 0 else "inverse")
    c8.metric("Rentabilidad (%)", f"{(ganancia_neta / capital_inicial * 100):.2f}%")

    st.divider()
    
    st.subheader("📋 Registro Detallado de Operaciones SMC")
    df_mostrar = df_operaciones.copy()
    df_mostrar.index = range(1, len(df_mostrar) + 1)
    
    df_mostrar['Apertura (NY)'] = df_mostrar['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M')
    df_mostrar['Cierre (NY)'] = df_mostrar['Cierre (NY)'].dt.strftime('%Y-%m-%d %H:%M')
    
    for col in ['Entrada', 'Stop Loss', 'Take Profit', 'PnL ($)', 'Balance', 'Liq_Max', 'Liq_Min']:
        df_mostrar[col] = df_mostrar[col].apply(lambda x: f"${x:,.2f}" if pd.notnull(x) else x)
        
    columnas_finales = ['Apertura (NY)', 'Cierre (NY)', 'Tipo', 'Entrada', 'Stop Loss', 'Take Profit', 'Resultado', 'PnL ($)', 'Balance']
    st.dataframe(df_mostrar[columnas_finales], use_container_width=True)
    
    st.divider()
    
    st.subheader("🔍 Visualizador de Patrón: Liquidez + ChoCh")
    opciones_trades = df_operaciones['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M').tolist()
    trade_seleccionado = st.selectbox("Selecciona la entrada para analizar el Sweep y el ChoCh:", opciones_trades)
    
    if trade_seleccionado:
        trade_data = df_operaciones[df_operaciones['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M') == trade_seleccionado].iloc[0]
        fecha_obj = trade_data['Apertura (NY)']
        dia_str = fecha_obj.strftime('%Y-%m-%d')
        
        # Mostramos desde las 06:00 AM NY (para ver la liquidez previa) hasta las 14:00
        df_dia = df_btc.loc[f"{dia_str} 06:00:00":f"{dia_str} 14:00:00"]
        
        fig = go.Figure(data=[go.Candlestick(
            x=df_dia.index, open=df_dia['Open'], high=df_dia['High'],
            low=df_dia['Low'], close=df_dia['Close'], name='BTC/USDT (1m)'
        )])
        
        # Líneas de Liquidez de Asia/Londres
        fig.add_hline(y=trade_data['Liq_Max'], line_dash="dash", line_color="orange", annotation_text="Liquidez Máxima (Buy Stops)")
        fig.add_hline(y=trade_data['Liq_Min'], line_dash="dash", line_color="orange", annotation_text="Liquidez Mínima (Sell Stops)")
        
        color_flecha = "#00FF00" if "Long" in trade_data['Tipo'] else "#FF0000"
        simbolo_flecha = "triangle-up" if "Long" in trade_data['Tipo'] else "triangle-down"
        
        # Punto de Entrada exacto en el Cambio de Estructura (ChoCh)
        fig.add_trace(go.Scatter(
            x=[fecha_obj], y=[trade_data['Entrada']], mode='markers', name='Entrada post-ChoCh',
            marker=dict(symbol=simbolo_flecha, size=15, color=color_flecha, line=dict(width=2, color='white'))
        ))
        
        fig.add_hline(y=trade_data['Stop Loss'], line_dash="solid", line_color="red", annotation_text="SL (Extremo del Sweep)")
        fig.add_hline(y=trade_data['Take Profit'], line_dash="solid", line_color="green", annotation_text="Take Profit")
        
        fig.update_layout(
            title=f"Trade Institucional {trade_data['Tipo']} | Toma de Liquidez: {dia_str} | Res: {trade_data['Resultado']}",
            yaxis_title="Precio Bitcoin (USD)", xaxis_title="Hora NY",
            height=650, xaxis_rangeslider_visible=False, template="plotly_dark",
            margin=dict(l=50, r=50, t=80, b=50)
        )
        
        st.plotly_chart(fig, use_container_width=True)