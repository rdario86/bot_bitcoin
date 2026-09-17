import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
from datetime import timedelta
import time

st.set_page_config(page_title="BOT ORB Scalping (Nasdaq)", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA
# ==========================================
st.sidebar.markdown("### ⏱️ Temporalidad")
# Selector externo para que el slider se actualice dinámicamente
temporalidad = st.sidebar.radio(
    "Resolución de Velas:", 
    ["5 Minutos (Máx 60 días)", "1 Minuto (Máx 7 días)"], 
    help="Yahoo Finance bloquea la descarga de 1 minuto más allá de los últimos 7 días. Usa 5 minutos para historial profundo."
)

es_1m = "1 Minuto" in temporalidad
max_dias = 7 if es_1m else 60
dias_defecto = 7 if es_1m else 30

with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Ajustes Scalping NQ")
    
    st.subheader("💰 Gestión de Capital")
    capital_inicial = st.number_input("Bank / Capital Inicial ($)", min_value=100.0, value=1000.0, step=100.0)
    riesgo_pct = st.selectbox("Riesgo por Operación (%)", options=[1, 2, 3, 4, 5], index=2)
    
    st.divider()
    
    # El slider ahora es dinámico dependiendo de la temporalidad elegida
    dias_historial = st.slider("Días de Backtesting", 1, max_dias, dias_defecto)
    
    opciones_ratio = {1.0: "1:1", 1.5: "1:1.50", 2.0: "1:2", 2.5: "1:2.50", 3.0: "1:3"}
    ratio_rr = st.selectbox("Ratio Riesgo/Beneficio", options=list(opciones_ratio.keys()), format_func=lambda x: opciones_ratio[x], index=4)
    
    ejecutar_btn = st.form_submit_button("Confirmar y Ejecutar")

# ==========================================
# 2. CONEXIÓN A YAHOO FINANCE
# ==========================================
@st.cache_data(ttl=300, show_spinner="Descargando datos del Nasdaq (NDX) desde Yahoo Finance...")
def obtener_datos_nasdaq(dias, es_1m):
    try:
        ticker = "^NDX" 
        intervalo = "1m" if es_1m else "5m"
        df = yf.download(ticker, period=f"{dias}d", interval=intervalo, progress=False)
        
        if df.empty:
            return pd.DataFrame()
            
        df.reset_index(inplace=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            
        df.rename(columns={'Datetime': 'Timestamp'}, inplace=True)
        df['Timestamp'] = pd.to_datetime(df['Timestamp'])
        
        if df['Timestamp'].dt.tz is None:
            df['Timestamp'] = df['Timestamp'].dt.tz_localize('America/New_York')
        else:
            df['Timestamp'] = df['Timestamp'].dt.tz_convert('America/New_York')
            
        df.set_index('Timestamp', inplace=True)
        df['Date'] = df.index.date
        
        return df
    except Exception as e:
        st.error(f"Error de conexión con Yahoo Finance: {e}")
        return pd.DataFrame()

# ==========================================
# 3. MOTOR DE BACKTESTING DUAL (1m / 5m)
# ==========================================
def ejecutar_backtest(df, ratio, capital_inicial, riesgo_pct, es_1m):
    operaciones = []
    if df.empty: return pd.DataFrame(operaciones)
        
    df_calc = df.copy()
    
    # RADAR DE ESTRUCTURA (10 minutos)
    # Si son velas de 1m miramos 10 velas. Si son de 5m miramos 2 velas.
    ventanas_pivote = 10 if es_1m else 2
    df_calc['Swing_Low'] = df_calc['Low'].rolling(window=ventanas_pivote).min()
    df_calc['Swing_High'] = df_calc['High'].rolling(window=ventanas_pivote).max()
        
    fechas = df_calc['Date'].unique()
    capital_actual = capital_inicial
    
    for fecha in fechas:
        if fecha.weekday() >= 5: continue 
            
        df_dia = df_calc[df_calc['Date'] == fecha]
        
        # 1. RANGO DE APERTURA
        if es_1m:
            vela_apertura = df_dia.between_time('09:30', '09:34')
            if len(vela_apertura) < 5: continue 
        else:
            # En temporalidad de 5m, la vela de las 09:30 ya contiene todo el rango ORB
            vela_apertura = df_dia.between_time('09:30', '09:30')
            if len(vela_apertura) < 1: continue 
            
        max_orb = float(vela_apertura['High'].max())
        min_orb = float(vela_apertura['Low'].min())
        
        # 2. BÚSQUEDA DE ENTRADA EXCLUSIVA (09:35 a 10:30)
        horario_operativo = df_dia.between_time('09:35', '10:30')
        
        for idx, row in horario_operativo.iterrows():
            entrada = float(row['Close']) 
            tipo_trade = None
            stop_loss = 0
            
            if entrada > max_orb:
                tipo_trade = 'Long 🟢'
                stop_loss = float(row['Swing_Low'])
                if stop_loss >= max_orb: stop_loss = min_orb 
                
            elif entrada < min_orb:
                tipo_trade = 'Short 🔴'
                stop_loss = float(row['Swing_High'])
                if stop_loss <= min_orb: stop_loss = max_orb
                
            if tipo_trade:
                riesgo_precio = abs(entrada - stop_loss)
                if riesgo_precio == 0: continue
                
                take_profit = entrada + (riesgo_precio * ratio) if "Long" in tipo_trade else entrada - (riesgo_precio * ratio)
                
                resultado = "Sin Resolución ⏳"
                pnl_usd = 0.0
                riesgo_usd = capital_actual * (riesgo_pct / 100)
                
                df_post_entrada = df_calc.loc[idx:]
                
                for jdx, vela in df_post_entrada.iterrows():
                    if jdx == idx: continue 
                    
                    vela_low = float(vela['Low'])
                    vela_high = float(vela['High'])
                    
                    if "Long" in tipo_trade:
                        if vela_low <= stop_loss:
                            resultado, pnl_usd = "Pérdida ❌", -riesgo_usd
                            break
                        elif vela_high >= take_profit:
                            resultado, pnl_usd = "Ganancia ✅", riesgo_usd * ratio
                            break
                    else: 
                        if vela_high >= stop_loss:
                            resultado, pnl_usd = "Pérdida ❌", -riesgo_usd
                            break
                        elif vela_low <= take_profit:
                            resultado, pnl_usd = "Ganancia ✅", riesgo_usd * ratio
                            break
                
                capital_actual += pnl_usd
                operaciones.append({
                    'Fecha': idx, 'Tipo': tipo_trade, 'Entrada': entrada,
                    'Stop Loss': stop_loss, 'Take Profit': take_profit,
                    'Resultado': resultado, 'Max_ORB_5m': max_orb, 'Min_ORB_5m': min_orb,
                    'PnL ($)': pnl_usd, 'Balance': capital_actual
                })
                break 

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS
# ==========================================
st.title("📈 BOT Estrategia ORB - Nasdaq (Scalping Estructural)")

df_nq = obtener_datos_nasdaq(dias_historial, es_1m)
df_operaciones = ejecutar_backtest(df_nq, ratio_rr, capital_inicial, riesgo_pct, es_1m)

if df_nq.empty:
    st.warning("No se pudieron cargar los datos del Nasdaq desde Yahoo Finance.")
elif df_operaciones.empty:
    st.info("No se encontraron operaciones en el rango seleccionado.")
else:
    total_trades = len(df_operaciones)
    balance_final = df_operaciones['Balance'].iloc[-1]
    ganancia_neta_total = balance_final - capital_inicial
    rentabilidad_total = (ganancia_neta_total / capital_inicial) * 100

    st.subheader("📊 Resumen de Rendimiento Nasdaq (SL Estructural)")
    
    aciertos_tot = len(df_operaciones[df_operaciones['Resultado'].str.contains("Ganancia")])
    fallos_tot = len(df_operaciones[df_operaciones['Resultado'].str.contains("Pérdida")])
    win_rate_tot = (aciertos_tot / total_trades) * 100 if total_trades > 0 else 0
    
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Operaciones", total_trades)
    c2.metric("Tocaron TP ✅", aciertos_tot)
    c3.metric("Tocaron SL ❌", fallos_tot)
    c4.metric("% Win Rate", f"{win_rate_tot:.1f}%")
    
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Capital Inicial", f"${capital_inicial:,.2f}")
    c6.metric("Balance Final", f"${balance_final:,.2f}")
    c7.metric("PnL Neto ($)", f"${ganancia_neta_total:,.2f}", delta_color="normal" if ganancia_neta_total >= 0 else "inverse")
    c8.metric("Rentabilidad Total (%)", f"{rentabilidad_total:.2f}%")

    st.divider()
    
    st.subheader("📋 Registro Detallado")
    df_mostrar = df_operaciones.copy()
    df_mostrar.index = range(1, len(df_mostrar) + 1)
    
    columnas_moneda = ['Entrada', 'Stop Loss', 'Take Profit', 'PnL ($)', 'Balance']
    for col in columnas_moneda:
        df_mostrar[col] = df_mostrar[col].apply(lambda x: f"-${abs(x):,.2f}" if pd.notnull(x) and x < 0 else f"${x:,.2f}" if pd.notnull(x) else x)
        
    columnas_finales = ['Fecha', 'Tipo', 'Entrada', 'Stop Loss', 'Take Profit', 'Resultado', 'PnL ($)', 'Balance']
    st.dataframe(df_mostrar[columnas_finales], use_container_width=True)
    
    st.divider()
    
    st.subheader("🔍 Visualizador de Scalping Nasdaq")
    opciones_trades = df_operaciones['Fecha'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist()
    trade_seleccionado = st.selectbox("Selecciona la fecha del Trade:", opciones_trades)
    
    if trade_seleccionado:
        trade_data = df_operaciones[df_operaciones['Fecha'].dt.strftime('%Y-%m-%d %H:%M:%S') == trade_seleccionado].iloc[0]
        fecha_obj = trade_data['Fecha']
        dia_str = fecha_obj.strftime('%Y-%m-%d')
        
        inicio_grafico = (fecha_obj - pd.Timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
        fin_grafico = (fecha_obj + pd.Timedelta(minutes=180)).strftime('%Y-%m-%d %H:%M:%S')
        df_dia = df_nq.loc[inicio_grafico:fin_grafico]
        
        fig = go.Figure(data=[go.Candlestick(
            x=df_dia.index,
            open=df_dia['Open'], high=df_dia['High'],
            low=df_dia['Low'], close=df_dia['Close'],
            name='NDX'
        )])
        
        fig.add_hline(y=trade_data['Max_ORB_5m'], line_dash="dash", line_color="blue", annotation_text="Máx 5m")
        fig.add_hline(y=trade_data['Min_ORB_5m'], line_dash="dash", line_color="blue", annotation_text="Mín 5m")
        
        color_flecha = "green" if "Long" in trade_data['Tipo'] else "red"
        simbolo_flecha = "triangle-up" if "Long" in trade_data['Tipo'] else "triangle-down"
        
        fig.add_trace(go.Scatter(
            x=[fecha_obj], y=[trade_data['Entrada']],
            mode='markers', name='Ruptura y Entrada',
            marker=dict(symbol=simbolo_flecha, size=15, color=color_flecha)
        ))
        
        fig.add_hline(y=trade_data['Stop Loss'], line_dash="solid", line_color="red", annotation_text="SL (Pivote Estructural)")
        fig.add_hline(y=trade_data['Take Profit'], line_dash="solid", line_color="green", annotation_text="Take Profit")
        
        fig.update_layout(
            title=f"Scalp Nasdaq {trade_data['Tipo']} el {dia_str} | Resultado: {trade_data['Resultado']}",
            yaxis_title="Precio NQ (USD)",
            xaxis_title="Hora (EST - NY)",
            height=600,
            xaxis_rangeslider_visible=False,
            template="plotly_dark"
        )
        
        st.plotly_chart(fig, use_container_width=True)
