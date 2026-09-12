import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta
import time

st.set_page_config(page_title="App Trading: ORB Bitcoin", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Ajustes ORB")
    
    # NUEVO: Selector de Rango de Apertura
    rango_orb = st.selectbox(
        "Duración del Rango ORB", 
        options=[5, 15, 30], 
        index=2, 
        format_func=lambda x: f"Primeros {x} Minutos",
        help="Define cuántos minutos después de la apertura de NY se usarán para establecer el máximo y mínimo del rango."
    )
    
    dias_historial = st.slider("Días de Backtesting", 1, 45, 30)
    ratio_rr = st.number_input("Ratio Riesgo/Beneficio (1:X)", value=2.0)
    
    st.divider()
    st.subheader("📏 Filtros de Ruptura (First Strike)")
    stop_loss_pct = st.number_input("Stop Loss Fijo (%)", min_value=0.05, max_value=10.0, value=0.50, step=0.05)
    
    rango_ruptura = st.slider(
        "Ruptura (% del Cuerpo por fuera)", 
        5, 100, (25, 50), 5, 
        help="Exige que la parte del cuerpo que rompe la línea represente entre un 25% y 50% del total. Si la PRIMERA vela que rompe no cumple esto, no se opera ese día."
    )
    
    fuerza_cuerpo = st.slider("Fuerza de la Vela (Cuerpo vs Mechas %)", 50, 100, 60, 5)
    
    ejecutar_btn = st.form_submit_button("Confirmar Ajustes y Ejecutar")

# ==========================================
# 2. CONEXIÓN A BINGX OPTIMIZADA
# ==========================================
@st.cache_data(ttl=300, show_spinner="Descargando datos de BingX Futuros (5m)...")
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
                velas = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='5m', since=since, limit=limite_velas)
                if not velas:
                    break
                
                todas_las_velas.extend(velas)
                since = velas[-1][0] + 300000 
                
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
# 3. MOTOR DE BACKTESTING (REGLA FIRST STRIKE MULTI-RANGO)
# ==========================================
def ejecutar_backtest(df, pct_cuerpo, ratio, rango_rup, sl_pct, minutos_rango):
    operaciones = []
    if df.empty:
        return pd.DataFrame(operaciones)
        
    fechas = df['Date'].unique()
    
    # Configuración dinámica de los tiempos según la elección del usuario
    if minutos_rango == 5:
        fin_rango = '09:34'
        inicio_operativo = '09:35'
        velas_req = 1
    elif minutos_rango == 15:
        fin_rango = '09:44'
        inicio_operativo = '09:45'
        velas_req = 3
    else: # 30 minutos
        fin_rango = '09:59'
        inicio_operativo = '10:00'
        velas_req = 6
    
    for fecha in fechas:
        df_dia = df[df['Date'] == fecha]
        
        rango_inicial = df_dia.between_time('09:30', fin_rango)
        if rango_inicial.empty or len(rango_inicial) < velas_req:
            continue
            
        max_orb = rango_inicial['High'].max()
        min_orb = rango_inicial['Low'].min()
        
        horario_operativo = df_dia.between_time(inicio_operativo, '12:00')
        
        for idx, row in horario_operativo.iterrows():
            tamaño_vela = row['High'] - row['Low']
            if tamaño_vela == 0: continue
                
            tamaño_cuerpo = abs(row['Open'] - row['Close'])
            if tamaño_cuerpo == 0: continue
                
            entrada = row['Close']
            tipo_trade = None
            
            # DETECCIÓN DE LA PRIMERA RUPTURA HACIA ARRIBA
            if entrada > max_orb:
                parte_fuera = entrada - max_orb
                pct_fuera = (parte_fuera / tamaño_cuerpo) * 100
                
                if (tamaño_cuerpo / tamaño_vela) >= (pct_cuerpo / 100):
                    if rango_rup[0] <= pct_fuera <= rango_rup[1]:
                        tipo_trade = 'Long 🟢'
                        stop_loss = entrada * (1 - (sl_pct / 100))
                        take_profit = entrada * (1 + ((sl_pct * ratio) / 100))
                
                if tipo_trade:
                    resultado = "Sin Resolución ⏳"
                    df_post_entrada = df_dia.loc[idx:]
                    for jdx, vela in df_post_entrada.iterrows():
                        if jdx == idx: continue 
                        
                        if vela['Low'] <= stop_loss:
                            resultado = "Pérdida ❌"
                            break
                        elif vela['High'] >= take_profit:
                            resultado = "Ganancia ✅"
                            break
                    
                    operaciones.append({
                        'Fecha': idx, 'Tipo': tipo_trade, 'Entrada': entrada,
                        'Stop Loss': stop_loss, 'Take Profit': take_profit,
                        'Resultado': resultado,
                        'Max_ORB': max_orb, 'Min_ORB': min_orb,
                        'Pct_Ruptura': pct_fuera
                    })
                break 
                    
            # DETECCIÓN DE LA PRIMERA RUPTURA HACIA ABAJO
            elif entrada < min_orb:
                parte_fuera = min_orb - entrada
                pct_fuera = (parte_fuera / tamaño_cuerpo) * 100
                
                if (tamaño_cuerpo / tamaño_vela) >= (pct_cuerpo / 100):
                    if rango_rup[0] <= pct_fuera <= rango_rup[1]:
                        tipo_trade = 'Short 🔴'
                        stop_loss = entrada * (1 + (sl_pct / 100))
                        take_profit = entrada * (1 - ((sl_pct * ratio) / 100))
                
                if tipo_trade:
                    resultado = "Sin Resolución ⏳"
                    df_post_entrada = df_dia.loc[idx:]
                    for jdx, vela in df_post_entrada.iterrows():
                        if jdx == idx: continue 
                        
                        if vela['High'] >= stop_loss:
                            resultado = "Pérdida ❌"
                            break
                        elif vela['Low'] <= take_profit:
                            resultado = "Ganancia ✅"
                            break
                    
                    operaciones.append({
                        'Fecha': idx, 'Tipo': tipo_trade, 'Entrada': entrada,
                        'Stop Loss': stop_loss, 'Take Profit': take_profit,
                        'Resultado': resultado,
                        'Max_ORB': max_orb, 'Min_ORB': min_orb,
                        'Pct_Ruptura': pct_fuera
                    })
                break 

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS
# ==========================================
st.title(f"📈 App de Estrategia ORB - Bitcoin ({rango_orb} Minutos)")

df_btc = obtener_datos_bingx(dias_historial)
df_operaciones = ejecutar_backtest(df_btc, fuerza_cuerpo, ratio_rr, rango_ruptura, stop_loss_pct, rango_orb)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos.")
elif df_operaciones.empty:
    st.info("No se encontraron operaciones válidas. La regla 'First Strike' descartó los días donde la primera ruptura no cumplió los requisitos.")
else:
    total_trades = len(df_operaciones)
    aciertos = len(df_operaciones[df_operaciones['Resultado'] == "Ganancia ✅"])
    fallos = len(df_operaciones[df_operaciones['Resultado'] == "Pérdida ❌"])
    win_rate = (aciertos / total_trades) * 100 if total_trades > 0 else 0
    
    st.subheader("📊 Resumen de Rendimiento")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Operaciones", total_trades)
    col2.metric("Aciertos ✅", aciertos)
    col3.metric("Fallos ❌", fallos)
    col4.metric("% Win Rate", f"{win_rate:.1f}%")
    
    st.divider()
    
    st.subheader("📋 Registro Detallado")
    df_mostrar = df_operaciones.copy()
    columnas_moneda = ['Entrada', 'Stop Loss', 'Take Profit']
    
    for col in columnas_moneda:
        df_mostrar[col] = df_mostrar[col].apply(lambda x: f"${x:,.2f}")
    
    df_mostrar['Ruptura (%)'] = df_mostrar['Pct_Ruptura'].apply(lambda x: f"{x:.1f}%")
    
    st.dataframe(df_mostrar[['Fecha', 'Tipo', 'Entrada', 'Stop Loss', 'Take Profit', 'Ruptura (%)', 'Resultado']], use_container_width=True)
    
    st.divider()
    
    st.subheader("🔍 Visualizador de Operaciones")
    opciones_trades = df_operaciones['Fecha'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist()
    trade_seleccionado = st.selectbox("Selecciona la fecha del Trade:", opciones_trades)
    
    if trade_seleccionado:
        trade_data = df_operaciones[df_operaciones['Fecha'].dt.strftime('%Y-%m-%d %H:%M:%S') == trade_seleccionado].iloc[0]
        fecha_obj = trade_data['Fecha']
        dia_str = fecha_obj.strftime('%Y-%m-%d')
        
        df_dia = df_btc.loc[f"{dia_str} 09:00:00":f"{dia_str} 14:00:00"]
        
        fig = go.Figure(data=[go.Candlestick(
            x=df_dia.index,
            open=df_dia['Open'], high=df_dia['High'],
            low=df_dia['Low'], close=df_dia['Close'],
            name='BTC/USDT'
        )])
        
        fig.add_hline(y=trade_data['Max_ORB'], line_dash="dash", line_color="blue", annotation_text=f"Max {rango_orb}m")
        fig.add_hline(y=trade_data['Min_ORB'], line_dash="dash", line_color="blue", annotation_text=f"Min {rango_orb}m")
        
        color_flecha = "green" if "Long" in trade_data['Tipo'] else "red"
        simbolo_flecha = "triangle-up" if "Long" in trade_data['Tipo'] else "triangle-down"
        
        fig.add_trace(go.Scatter(
            x=[fecha_obj], y=[trade_data['Entrada']],
            mode='markers', name='Punto de Entrada',
            marker=dict(symbol=simbolo_flecha, size=15, color=color_flecha)
        ))
        
        fig.add_hline(y=trade_data['Stop Loss'], line_dash="solid", line_color="red", annotation_text=f"Stop Loss ({stop_loss_pct}%)")
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
