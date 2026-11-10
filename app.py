import streamlit as st
import sqlite3
import urllib.parse
import pandas as pd
from datetime import datetime

# 1. Inicializar la Base de Datos SQLite Local
def init_db():
    conn = sqlite3.connect('vetpulse.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS mascotas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            telefono_dueno TEXT NOT NULL,
            proxima_vacuna TEXT NOT NULL,
            hora_cita TEXT NOT NULL DEFAULT '12:00 AM',
            tipo_alerta TEXT NOT NULL DEFAULT 'Vacunación',
            notificado INTEGER DEFAULT 0,
            fecha_notificado TEXT
        )
    ''')
    try: cursor.execute("ALTER TABLE mascotas ADD COLUMN hora_cita TEXT NOT NULL DEFAULT '12:00 AM'")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE mascotas ADD COLUMN tipo_alerta TEXT NOT NULL DEFAULT 'Vacunación'")
    except sqlite3.OperationalError: pass
    conn.commit()
    conn.close()

init_db()

# 2. Configurar la Interfaz Corporativa en Streamlit
st.set_page_config(page_title="Goupets Pro Enterprise", page_icon="🐾", layout="wide")

if "autenticado" not in st.session_state:
    st.session_state["autenticado"] = False

logo_subido = None

# --- BARRA LATERAL (Clave: admin123) ---
with st.sidebar:
    st.title("⚙️ Goupets Config")
    st.markdown("---")
    if not st.session_state["autenticado"]:
        password = st.text_input("Contraseña del Panel", type="password")
        if st.button("Iniciar Sesión", use_container_width=True):
            if password == "admin123":
                st.session_state["autenticado"] = True
                st.success("¡Acceso concedido!")
                st.rerun()
            else: st.error("Contraseña incorrecta")
    else:
        st.success("🔒 Sesión Autorizada")
        st.markdown("---")
        st.subheader("🎨 Identidad del Negocio")
        logo_subido = st.file_uploader("Subir logo de la clínica", type=["png", "jpg", "jpeg"])
        if st.button("Cerrar Sesión", use_container_width=True):
            st.session_state["autenticado"] = False
            st.rerun()

# --- CUERPO PRINCIPAL ---
if logo_subido is not None: st.image(logo_subido, width=120)
else: st.markdown("<h3>🐾 Goupets - Gestión Veterinaria</h3>", unsafe_allow_html=True)

st.title("🐾 Goupets - Centro de Control e Ingresos")
st.markdown("---")

# Crear las pestañas operativas usando variables de contexto independientes
t_reg, t_sem, t_cop, t_met = st.tabs(["📋 Registrar Paciente", "🚦 Semáforo de Alertas", "🤖 IA Copilot (Seguimiento)", "📊 Historial y Métricas"])

# PESTAÑA 1: Formulario de Registro con Selección de Hora AM/PM
with t_reg:
    st.subheader("Añadir Nueva Cita/Recordatorio")
    with st.form("registro_mascota", clear_on_submit=True):
        nombre = st.text_input("Nombre de la mascota (o ID de rastreo)")
        telefono = st.text_input("Teléfono del dueño (ej. 573117920332)")
        tipo_alerta = st.selectbox("Motivo", ["Vacunación", "Desparasitación", "Control Médico", "Peluquería y Estética", "Cirugía / Postoperatorio"])
        
        col_f1, col_f2 = st.columns(2)
        with col_f1: fecha_vacuna = st.date_input("Fecha programada")
        with col_f2: hora_raw = st.time_input("Hora programada", value=datetime.strptime("08:00", "%H:%M").time(), step=60)
        
        enviado = st.form_submit_button("Guardar Alerta")
        if enviado and nombre and telefono:
            telefono_limpio = telefono.replace("+", "").replace(" ", "")
            hora_formateada = hora_raw.strftime("%I:%M %p")
            
            conn = sqlite3.connect('vetpulse.db')
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO mascotas (nombre, telefono_dueno, proxima_vacuna, hora_cita, tipo_alerta) VALUES (?, ?, ?, ?, ?)",
                (nombre, telefono_limpio, fecha_vacuna.strftime('%Y-%m-%d'), hora_formateada, tipo_alerta)
            )
            conn.commit()
            conn.close()
            st.success("¡Guardado con éxito en Goupets!")
            st.rerun()

# PESTAÑA 2: Panel Inteligente (Semáforo de Citas con Hora AM/PM)
with t_sem:
    st.subheader("🚦 Panel de Control de Recordatorios")
    conn = sqlite3.connect('vetpulse.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, nombre, telefono_dueno, proxima_vacuna, hora_cita, tipo_alerta FROM mascotas WHERE notificado = 0")
    pacientes = cursor.fetchall()
    conn.close()
    
    if not pacientes: st.info("No hay alertas pendientes en Goupets.")
    else:
        hoy = datetime.today()
        for p_id, p_nombre, p_telefono, p_fecha, p_hora, p_tipo in pacientes:
            fecha_cita = datetime.strptime(p_fecha, '%Y-%m-%d')
            dias_restantes = (fecha_cita - hoy).days + 1
            
            if dias_restantes < 0:
                estado_texto = f"🔴 VENCIDO (Hace {abs(dias_restantes)} días)"
                alerta_ui = st.error
            elif dias_restantes <= 1:
                estado_texto = "🟡 URGENTE"
                alerta_ui = st.warning
            else:
                estado_texto = f"🟢 Al día (Faltan {dias_restantes} días)"
                alerta_ui = st.info
                
            col1, col2, col3 = st.columns(3)
            with col1:
                st.markdown(f"### 🐕 {p_nombre}")
                alerta_ui(f"**Motivo:** {p_tipo} | {estado_texto} \n\n **Programado:** {p_fecha} a las {p_hora}")
            with col2:
                msg = f"Hola, recordatorio de {p_tipo} para *{p_nombre}* el día {p_fecha} a las {p_hora}. Anticipa tu pago aquí para apartar tu turno: https://wompi.co"
                url_whatsapp = f"https://wa.me{p_telefono}?" + urllib.parse.urlencode({'text': msg})
                st.link_button(f"📲 Enviar Alerta", url_whatsapp, use_container_width=True)
            with col3:
                st.write(""); st.write("")
                if st.button("✅ Listo", key=f"done_{p_id}", use_container_width=True):
                    conn = sqlite3.connect('vetpulse.db')
                    cursor = conn.cursor()
                    ahora = datetime.now().strftime('%Y-%m-%d %H:%M')
                    cursor.execute("UPDATE mascotas SET notificado = 1, fecha_notificado = ? WHERE id = ?", (ahora, p_id))
                    conn.commit()
                    conn.close()
                    st.rerun()

# PESTAÑA 3: IA Copilot (Seguimiento)
with t_cop:
    if st.session_state["autenticado"]:
        st.subheader("🤖 IA Copilot - Redactor Clínico Goupets")
        col_ia1, col_ia2 = st.columns(2)
        with col_ia1:
            ia_nombre = st.text_input("Nombre de la mascota:", placeholder="Ej. Max")
            ia_telefono = st.text_input("WhatsApp del dueño:", placeholder="Ej. 573117920332")
            ia_diag = st.selectbox("Procedimiento:", ["Postoperatorio Castración", "Tratamiento Gastroenteritis", "Higiene Oral Profilaxis"])
            generar_ia = st.button("✨ Generar Mensaje Clínico", use_container_width=True)
        with col_ia2:
            if generar_ia and ia_nombre and ia_telefono:
                biblioteca = {
                    "Postoperatorio Castración": f"🏥 Reporte Goupets: Hola, ¿cómo evoluciona *{ia_nombre}* de su cirugía? Recuerda: 1. Dejar collar isabelino. 2. Puntos limpios. 3. Suministrar analgésicos.",
                    "Tratamiento Gastroenteritis": f"🐾 Seguimiento Goupets: Hola, ¿cómo sigue *{ia_nombre}*? Recuerda: 1. Agua en sorbos pequeños. 2. Dieta blanda tras 6 horas sin vomitar.",
                    "Higiene Oral Profilaxis": f"🦷 Cuidado Oral Goupets: Hola, cuidados de *{ia_nombre}* poslimpieza dental: 1. Alimento blando hoy. 2. Evitar juguetes rígidos."
                }
                texto_ia = biblioteca.get(ia_diag, "Seguimiento médico.")
                st.text_area("📋 Copilot Redacción:", value=texto_ia, height=150)
                url_ia = f"https://wa.me{ia_telefono}?" + urllib.parse.urlencode({'text': texto_ia})
                st.link_button("📲 Despachar Reporte", url_ia, use_container_width=True)
            elif generar_ia: st.error("Por favor rellena todos los campos.")
    else: st.warning("⚠️ Ingresa la contraseña administrativa `admin123` en la barra lateral para desbloquear el IA Copilot de Goupets.")

# PESTAÑA 4: Historial y Métricas Estables (OBLIGATORIA Y TOTALMENTE CERRADA)
with t_met:
    if st.session_state["autenticado"]:
        st.subheader("📊 Panel de Control Administrativo - Goupets")
        conn = sqlite3.connect('vetpulse.db')
        cursor = conn.cursor()
        
        # Desempaquetado correcto de tuplas numéricas para los KPIs
        p_res = cursor.execute("SELECT COUNT(*) FROM mascotas WHERE notificado = 0").fetchone()[0]
        e_res = cursor.execute("SELECT COUNT(*) FROM mascotas WHERE notificado = 1").fetchone()[0]
        
        df_tot = pd.read_sql_query("SELECT nombre as Mascota, telefono_dueno as Teléfono, tipo_alerta as [Tipo] FROM mascotas", conn)
        df_env = pd.read_sql_query("SELECT nombre as Mascota, telefono_dueno as Teléfono, tipo_alerta as [Tipo], proxima_vacuna as [Fecha Cita], hora_cita as [Hora Cita], fecha_notificado as [Fecha Envío] FROM mascotas WHERE notificado = 1", conn)
        conn.close()
        
        kpi1, kpi2, kpi3 = st.columns(3)
        kpi1.metric(label="📈 Alertas Totales", value=len(df_tot))
        kpi2.metric(label="⏳ Pendientes", value=p_res)
        kpi3.metric(label="✅ Mensajes Enviados", value=e_res)
        
        st.markdown("---")
        st.subheader("📋 Detalle de Auditoría Externa")
        
        # Se imprime directamente el dataframe completo de forma obligatoria e independiente
        st.dataframe(df_env, use_container_width=True)
        
        # Descarga directa
        csv_bytes = df_env.to_csv(index=False).encode('utf-8')
