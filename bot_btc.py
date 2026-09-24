import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta
import time

st.set_page_config(page_title="BOT Estrategia ORB - Comparativa BTC vs ETH", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA Y CAPITAL
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Ajustes ORB (Comparativa)")
    st.markdown("""
    **Estrategia:**
    - Rango: 09:30 - 09:34 NY
    - Stop Loss: Dinámico estructural (Pullback Swing)
    - Activos: BTC/USDT & ETH/USDT
    """)
    
    st.subheader("💰 Gestión de Capital")
    capital_inicial = st.number_input("Bank / Capital Inicial ($)", min_value=100.0, value=1000.0, step=100.0)
    riesgo_pct = st.selectbox("Riesgo por Operación (%)", options=[1, 2, 3, 4, 5], index=2)
    
    st.divider()
    
    dias_historial = st.slider("Días de Backtesting", 1, 30, 15)
    
    opciones_ratio = {1.0: "1:1", 1.5: "1:1.50", 2.0: "1:2", 2.5: "1:2.50", 3.0: "1:3"}
    ratio_rr = st.selectbox(
        "Ratio Riesgo/Beneficio", 
        options=list(opciones_ratio.keys()), 
        format_func=lambda x: opciones_ratio[x],
        index=3
    )
    
    ejecutar_btn = st.form_submit_button("Confirmar y Ejecutar Comparativa")

# ==========================================
# 2. CONEXIÓN Y DESCARGA MULTI-ACTIVO (BINGX)
# ==========================================
@st.cache_data(ttl=300, show_spinner="Descargando histórico de BingX para BTC y ETH...")
def obtener_datos_multi(dias):
    try:
        exchange = ccxt.bingx({
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'}
        })
        
        ahora = pd.Timestamp.utcnow()
        inicio = ahora - pd.Timedelta(days=dias)
        since = exchange.parse8601(inicio.isoformat())
        
        simbolos = ['BTC/USDT:USDT', 'ETH/USDT:USDT']
        datos_mercado = {}
        
        for simbolo in simbolos:
            todas_las_velas = []
            temp_since = since
            limite_velas = 1000 
            
            while True:
                try:
                    velas = exchange.fetch_ohlcv(simbolo, timeframe='1m', since=temp_since, limit=limite_velas)
                    if not velas: break
                    todas_las_velas.extend(velas)
                    temp_since = velas[-1][0] + 60000 
                    if len(velas) < limite_velas: break 
                    time.sleep(0.1) 
                except Exception:
                    break
                    
            if todas_las_velas:
                df = pd.DataFrame(todas_las_velas, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
                df.set_index('Timestamp', inplace=True)
                df.index = df.index.tz_localize('UTC').tz_convert('America/New_York')
                df = df[~df.index.duplicated(keep='first')]
                df['Date'] = df.index.date
                datos_mercado[simbolo.split('/')[0]] = df
                
        return datos_mercado
    
    except Exception as e:
        st.error(f"Error crítico de conexión: {e}")
        return {}

# ==========================================
# 3. MOTOR DE BACKTESTING (STOP DINÁMICO)
# ==========================================
def ejecutar_backtest_asset(df, ratio, capital_inicial, riesgo_pct):
    operaciones = []
    if df.empty: return pd.DataFrame(operaciones)
        
    df_calc = df.copy()
    fechas = df_calc['Date'].unique()
    capital_actual = capital_inicial
    
    for fecha in fechas:
        if fecha.weekday() >= 5: continue 
            
        hora_inicio_orb = pd.to_datetime('09:30').time()
        hora_fin_orb = pd.to_datetime('09:34').time()
        
        velas_orb = df_calc.loc[(df_calc['Date'] == fecha) & (df_calc.index.time >= hora_inicio_orb) & (df_calc.index.time <= hora_fin_orb)]
        
        if len(velas_orb) == 5: 
            max_orb = velas_orb['High'].max()
            min_orb = velas_orb['Low'].min()
            
            horario_us = df_calc.loc[(df_calc['Date'] == fecha) & (df_calc.index.time >= pd.to_datetime('09:35').time()) & (df_calc.index.time < pd.to_datetime('10:30').time())]
            
            estado_ruptura = None
            pullback_hecho = False
            sl_estructural = None 
            
            for idx, row in horario_us.iterrows():
                if estado_ruptura is None:
                    cierre = row['Close']
                    if cierre > max_orb:
                        estado_ruptura = 'Long'
                        sl_estructural = row['Low']
                    elif cierre < min_orb:
                        estado_ruptura = 'Short'
                        sl_estructural = row['High']
                else:
                    entrada = None
                    tipo_trade = None
                    stop_loss = 0
                    
                    if estado_ruptura == 'Long':
                        sl_estructural = min(sl_estructural, row['Low'])
                        if not pullback_hecho and row['Low'] <= max_orb:
                            pullback_hecho = True
                        if pullback_hecho and row['Close'] > max_orb:
                            entrada = row['Close']
                            tipo_trade = 'Long 🟢'
                            stop_loss = sl_estructural
                            
                    elif estado_ruptura == 'Short':
                        sl_estructural = max(sl_estructural, row['High'])
                        if not pullback_hecho and row['High'] >= min_orb:
                            pullback_hecho = True
                        if pullback_hecho and row['Close'] < min_orb:
                            entrada = row['Close']
                            tipo_trade = 'Short 🔴'
                            stop_loss = sl_estructural
                            
                    if entrada is not None:
                        riesgo_precio = abs(entrada - stop_loss)
                        if riesgo_precio == 0: riesgo_precio = 0.01 
                        
                        porcentaje_sl = (riesgo_precio / entrada) * 100
                        take_profit = entrada + (riesgo_precio * ratio) if "Long" in tipo_trade else entrada - (riesgo_precio * ratio)
                        
                        resultado = "Sin Resolución ⏳"
                        riesgo_usd = capital_actual * (riesgo_pct / 100)
                        df_post = df_calc.loc[idx + pd.Timedelta(minutes=1):]
                        limite_tiempo = idx.replace(hour=11, minute=0, second=0)
                        fecha_cierre = None 
                        
                        for jdx, vela in df_post.iterrows():
                            if jdx >= limite_tiempo:
                                precio_cierre = vela['Open'] 
                                dist = (precio_cierre - entrada) if "Long" in tipo_trade else (entrada - precio_cierre)
                                pnl_usd = (dist / riesgo_precio) * riesgo_usd
                                resultado = "Ganancia (11:00) ⏱️✅" if pnl_usd > 0 else "Pérdida (11:00) ⏱️❌"
                                fecha_cierre = jdx 
                                break
                                
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
                            'Apertura (NY)': idx, 'Cierre (NY)': fecha_cierre, 'Tipo': tipo_trade, 
                            'Entrada': entrada, 'Stop Loss': stop_loss, 'Tamaño SL (%)': porcentaje_sl, 
                            'Take Profit': take_profit, 'Resultado': resultado, 'Max_ORB': max_orb, 
                            'Min_ORB': min_orb, 'PnL ($)': pnl_usd, 'Balance': capital_actual
                        })
                        break 

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS COMPARATIVOS
# ==========================================
st.title("📈 Backtesting ORB: Comparativa Bitcoin vs Ethereum")

datos_mercado = obtener_datos_multi(dias_historial)

if not datos_mercado:
    st.warning("No se pudieron cargar los datos de los mercados.")
else:
    df_btc = datos_mercado.get('BTC', pd.DataFrame())
    df_eth = datos_mercado.get('ETH', pd.DataFrame())
    
    ops_btc = ejecutar_backtest_asset(df_btc, ratio_rr, capital_inicial, riesgo_pct)
    ops_eth = ejecutar_backtest_asset(df_eth, ratio_rr, capital_inicial, riesgo_pct)
    
    # Pestañas de visualización
    tab_comp, tab_btc, tab_eth = st.tabs(["📊 Comparativa Directa", "₿ Bitcoin (BTC)", "Ξ Ethereum (ETH)"])
    
    with tab_comp:
        st.subheader("⚔️ Enfrentamiento de Rendimiento")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("### ₿ Bitcoin (BTC)")
            if not ops_btc.empty:
                tot_b = len(ops_btc)
                win_b = len(ops_btc[ops_btc['Resultado'].str.contains("Ganancia")])
                wr_b = (win_b / tot_b) * 100
                pnl_b = ops_btc['PnL ($)'].sum()
                rent_b = (pnl_b / capital_inicial) * 100
                
                st.metric("Trades Totales", tot_b)
                st.metric("Win Rate", f"{wr_b:.1f}%")
                st.metric("PnL Neto ($)", f"${pnl_b:,.2f}", delta_color="normal" if pnl_b >= 0 else "inverse")
                st.metric("Rentabilidad", f"{rent_b:.2f}%")
            else:
                st.info("Sin operaciones para BTC.")
                
        with col2:
            st.markdown("### Ξ Ethereum (ETH)")
            if not ops_eth.empty:
                tot_e = len(ops_eth)
                win_e = len(ops_eth[ops_eth['Resultado'].str.contains("Ganancia")])
                wr_e = (win_e / tot_e) * 100
                pnl_e = ops_eth['PnL ($)'].sum()
                rent_e = (pnl_e / capital_inicial) * 100
                
                st.metric("Trades Totales", tot_e)
                st.metric("Win Rate", f"{wr_e:.1f}%")
                st.metric("PnL Neto ($)", f"${pnl_e:,.2f}", delta_color="normal" if pnl_e >= 0 else "inverse")
                st.metric("Rentabilidad", f"{rent_e:.2f}%")
            else:
                st.info("Sin operaciones para ETH.")

    with tab_btc:
        st.subheader("📋 Detalle de Operaciones - Bitcoin")
        if not ops_btc.empty:
            df_m_b = ops_btc.copy()
            df_m_b.index = range(1, len(df_m_b) + 1)
            df_m_b['Apertura (NY)'] = df_m_b['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M')
            df_m_b['Cierre (NY)'] = df_m_b['Cierre (NY)'].dt.strftime('%Y-%m-%d %H:%M')
            st.dataframe(df_m_b[['Apertura (NY)', 'Cierre (NY)', 'Tipo', 'Entrada', 'Stop Loss', 'Take Profit', 'Resultado', 'PnL ($)', 'Balance']], use_container_width=True)
        else:
            st.info("No hay registros.")

    with tab_eth:
        st.subheader("📋 Detalle de Operaciones - Ethereum")
        if not ops_eth.empty:
            df_m_e = ops_eth.copy()
            df_m_e.index = range(1, len(df_m_e) + 1)
            df_m_e['Apertura (NY)'] = df_m_e['Apertura (NY)'].dt.strftime('%Y-%m-%d %H:%M')
            df_m_e['Cierre (NY)'] = df_m_e['Cierre (NY)'].dt.strftime('%Y-%m-%d %H:%M')
            st.dataframe(df_m_e[['Apertura (NY)', 'Cierre (NY)', 'Tipo', 'Entrada', 'Stop Loss', 'Take Profit', 'Resultado', 'PnL ($)', 'Balance']], use_container_width=True)
        else:
            st.info("No hay registros.")