import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta
import time
import math

st.set_page_config(page_title="BOT Estrategia Rango + Imbalance", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA Y CAPITAL
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Estrategia Rango + FVG")
    st.markdown("""
    **Estrategia SMC:**
    1. Rango 15m (11:00 - 11:14).
    2. Ruptura con cuerpo en vela 5m.
    3. Buscar Imbalance (FVG) de 5m dentro del rango.
    4. Entrada Limit al tocar el FVG. (Sin FVG = Sin Trade).
    5. Cierre 16:00 NY.
    """)
    
    st.subheader("💰 Gestión de Capital")
    capital_inicial = st.number_input("Bank / Capital Inicial ($)", min_value=100.0, value=1000.0, step=100.0)
    riesgo_pct = st.selectbox("Riesgo por Operación (%)", options=[1, 2, 3, 4, 5], index=2)
    
    st.divider()
    
    dias_historial = st.slider("Días de Backtesting", 1, 30, 15)
    
    opciones_ratio = {1.0: "1:1", 1.5: "1:1.50", 2.0: "1:2", 2.5: "1:2.50", 3.0: "1:3"}
    ratio_rr = st.selectbox("Ratio Riesgo/Beneficio", options=list(opciones_ratio.keys()), index=3, format_func=lambda x: opciones_ratio[x])
    
    ejecutar_btn = st.form_submit_button("Confirmar y Ejecutar")

# ==========================================
# 2. CONEXIÓN A BINGX 
# ==========================================
@st.cache_data(ttl=300, show_spinner="Descargando histórico de BingX (1m)...")
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
                break
                
        if not todas_las_velas: return pd.DataFrame()
            
        df = pd.DataFrame(todas_las_velas, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Timestamp', inplace=True)
        df.index = df.index.tz_localize('UTC').tz_convert('America/New_York')
        df = df[~df.index.duplicated(keep='first')]
        df['Date'] = df.index.date
        return df
    except Exception as e:
        st.error(f"Error crítico: {e}")
        return pd.DataFrame()

# ==========================================
# 3. MOTOR DE BACKTESTING CON LÓGICA DE FVG (IMBALANCES)
# ==========================================
def ejecutar_backtest(df, ratio, capital_inicial, riesgo_pct):
    operaciones = []
    if df.empty: return pd.DataFrame(operaciones)
        
    # Crear un DataFrame paralelo en temporalidad de 5 minutos para buscar los FVGs
    df_5m = df.resample('5min').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}).dropna()
    
    fechas = df['Date'].unique()
    capital_actual = capital_inicial
    
    for fecha in fechas:
        if fecha.weekday() >= 5: continue 
            
        hora_inicio_rango = pd.to_datetime('11:00').time()
        hora_fin_rango = pd.to_datetime('11:14').time()
        
        velas_rango_1m = df.loc[(df['Date'] == fecha) & (df.index.time >= hora_inicio_rango) & (df.index.time <= hora_fin_rango)]
        
        if len(velas_rango_1m) >= 15:
            max_rango = velas_rango_1m['High'].max()
            min_rango = velas_rango_1m['Low'].min()
            
            # Extraer solo las velas de 5m de ese día a partir de las 11:00
            df_5m_dia = df_5m.loc[(df_5m.index.date == fecha) & (df_5m.index.time >= hora_inicio_rango) & (df_5m.index.time < pd.to_datetime('16:00').time())]
            
            estado_ruptura = None
            tiempo_ruptura = None
            nivel_fvg = None
            
            for i in range(2, len(df_5m_dia)):
                vela_1 = df_5m_dia.iloc[i-2]
                vela_2 = df_5m_dia.iloc[i-1]
                vela_3 = df_5m_dia.iloc[i] # Vela actual evaluándose para ruptura
                
                cierre_5m = vela_3['Close']
                
                # FASE 1: BÚSQUEDA DE RUPTURA
                if estado_ruptura is None:
                    if cierre_5m > max_rango:
                        estado_ruptura = 'Long'
                        tiempo_ruptura = vela_3.name
                    elif cierre_5m < min_rango:
                        estado_ruptura = 'Short'
                        tiempo_ruptura = vela_3.name
                        
                    # Si acabamos de detectar ruptura, buscamos FVGs hacia atrás
                    if estado_ruptura is not None:
                        # Buscamos en todas las velas de 5m desde las 11:00 hasta la vela de ruptura
                        velas_hasta_ruptura = df_5m_dia.loc[:tiempo_ruptura]
                        
                        fvg_encontrado = None
                        
                        # Recorremos de atrás hacia adelante para encontrar el imbalance más reciente
                        for j in range(len(velas_hasta_ruptura) - 1, 1, -1):
                            c1 = velas_hasta_ruptura.iloc[j-2]
                            c3 = velas_hasta_ruptura.iloc[j]
                            
                            if estado_ruptura == 'Long':
                                # FVG Alcista: Low de C3 > High de C1
                                if c3['Low'] > c1['High']:
                                    fvg_top = c3['Low']
                                    fvg_bottom = c1['High']
                                    # Verificar que esté DENTRO del rango (su parte alta o baja entra en el rango 15m)
                                    if fvg_top <= max_rango and fvg_bottom >= min_rango:
                                        fvg_encontrado = fvg_top # Entramos al tocar el inicio del gap
                                        break
                                        
                            elif estado_ruptura == 'Short':
                                # FVG Bajista: High de C3 < Low de C1
                                if c3['High'] < c1['Low']:
                                    fvg_bottom = c3['High']
                                    fvg_top = c1['Low']
                                    if fvg_bottom >= min_rango and fvg_top <= max_rango:
                                        fvg_encontrado = fvg_bottom # Entramos al tocar el inicio del gap
                                        break
                        
                        nivel_fvg = fvg_encontrado
                        
                        if nivel_fvg is None:
                            # Si rompió pero no hay FVG, se anula el día
                            break 
                            
                # FASE 2: RETESTEO AL IMBALANCE EN 1 MINUTO
                if estado_ruptura is not None and nivel_fvg is not None:
                    # Usamos el dataframe de 1 minuto desde el cierre de la vela de ruptura de 5m hasta las 16:00
                    horario_post_ruptura = df.loc[(df.index > tiempo_ruptura) & (df['Date'] == fecha) & (df.index.time < pd.to_datetime('16:00').time())]
                    
                    entrada = None
                    stop_loss = min_rango if estado_ruptura == 'Long' else max_rango
                    tipo_trade = f"{estado_ruptura} 🟢" if estado_ruptura == 'Long' else f"{estado_ruptura} 🔴"
                    
                    for idx, row in horario_post_ruptura.iterrows():
                        if estado_ruptura == 'Long' and row['Low'] <= nivel_fvg:
                            entrada = nivel_fvg
                        elif estado_ruptura == 'Short' and row['High'] >= nivel_fvg:
                            entrada = nivel_fvg
                            
                        # EJECUCIÓN
                        if entrada is not None:
                            riesgo_precio = abs(entrada - stop_loss)
                            if riesgo_precio == 0: riesgo_precio = 0.01 
                            
                            porcentaje_sl = (riesgo_precio / entrada) * 100
                            take_profit = entrada + (riesgo_precio * ratio) if estado_ruptura == 'Long' else entrada - (riesgo_precio * ratio)
                            
                            resultado = "Sin Resolución ⏳"
                            riesgo_usd = capital_actual * (riesgo_pct / 100)
                            
                            df_post = horario_post_ruptura.loc[idx:]
                            limite_tiempo = idx.replace(hour=16, minute=0, second=0)
                            fecha_cierre = None 
                            
                            for jdx, vela in df_post.iterrows():
                                if jdx >= limite_tiempo:
                                    precio_cierre = vela['Open'] 
                                    dist = (precio_cierre - entrada) if estado_ruptura == 'Long' else (entrada - precio_cierre)
                                    pnl_usd = (dist / riesgo_precio) * riesgo_usd
                                    resultado = "Ganancia (16:00) ⏱️✅" if pnl_usd > 0 else "Pérdida (16:00) ⏱️❌"
                                    fecha_cierre = jdx 
                                    break
                                    
                                if (estado_ruptura == 'Long' and vela['Low'] <= stop_loss) or (estado_ruptura == 'Short' and vela['High'] >= stop_loss):
                                    resultado, pnl_usd = "Pérdida ❌", -riesgo_usd
                                    fecha_cierre = jdx 
                                    break
                                elif (estado_ruptura == 'Long' and vela['High'] >= take_profit) or (estado_ruptura == 'Short' and vela['Low'] <= take_profit):
                                    resultado, pnl_usd = "Ganancia ✅", riesgo_usd * ratio
                                    fecha_cierre = jdx 
                                    break
                            
                            capital_actual += pnl_usd
                            operaciones.append({
                                'Apertura (NY)': idx, 'Cierre (NY)': fecha_cierre, 'Tipo': tipo_trade, 
                                'Entrada (FVG)': entrada, 'Stop Loss': stop_loss, 'Tamaño SL (%)': porcentaje_sl, 
                                'Take Profit': take_profit, 'Resultado': resultado, 'PnL ($)': pnl_usd, 'Balance': capital_actual
                            })
                            break 
                    break 

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS
# ==========================================
st.title("📈 BOT Estrategia Rango + Imbalance (FVG)")

df_btc = obtener_datos_bingx(dias_historial)
df_operaciones = ejecutar_backtest(df_btc, ratio_rr, capital_inicial, riesgo_pct)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos.")
elif df_operaciones.empty:
    st.info("No hubo operaciones. En los días evaluados o no hubo ruptura, o tras la ruptura NO había ningún Imbalance (FVG) de 5m dentro del rango.")
else:
    st.subheader(f"📊 Resumen de Rendimiento")
    
    total_trades = len(df_operaciones)
    aciertos = len(df_operaciones[df_operaciones['Resultado'].str.contains("Ganancia")])
    fallos = len(df_operaciones[df_operaciones['Resultado'].str.contains("Pérdida")])
    win_rate = (aciertos / total_trades) * 100
    ganancia_neta = df_operaciones['PnL ($)'].sum()
    balance_final = capital_inicial + ganancia_neta
    
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Trades FVG", total_trades)
    c2.metric("Aciertos ✅", aciertos)
    c3.metric("Fallos ❌", fallos)
    c4.metric("% Win Rate", f"{win_rate:.1f}%")
    
    c5, c6, c7 = st.columns(3)
    c5.metric("Balance Proyectado", f"${balance_final:,.2f}")
    c6.metric("PnL Neto ($)", f"${ganancia_neta:,.2f}", delta_color="normal" if ganancia_neta >= 0 else "inverse")
    c7.metric("Rentabilidad (%)", f"{(ganancia_neta / capital_inicial) * 100:.2f}%")
    
    st.divider()
    
    st.subheader("📋 Registro Detallado (Entradas en FVG)")
    df_mostrar = df_operaciones.copy()
    df_mostrar.index = range(1, len(df_mostrar) + 1)
    
    df_mostrar['Apertura (NY)'] = df_mostrar['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M')
    df_mostrar['Cierre (NY)'] = df_mostrar['Cierre (NY)'].dt.strftime('%Y-%m-%d %H:%M')
    
    for col in ['Entrada (FVG)', 'Stop Loss', 'Take Profit', 'PnL ($)', 'Balance']:
        df_mostrar[col] = df_mostrar[col].apply(lambda x: f"-${abs(x):,.2f}" if pd.notnull(x) and x < 0 else f"${x:,.2f}" if pd.notnull(x) else x)
        
    df_mostrar['Tamaño SL (%)'] = df_mostrar['Tamaño SL (%)'].apply(lambda x: f"{x:.2f}%")
    st.dataframe(df_mostrar, use_container_width=True)
