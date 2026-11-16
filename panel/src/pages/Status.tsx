export default function StatusPage({ title, description }: { title: string; description: string }) {
  return (
    <>
      <h2>{title}</h2>
      <div className="notice">
        <strong>Статус: частично / code-managed</strong>
        <p style={{ margin: "8px 0 0" }}>{description}</p>
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
          Раздел не имитирует работу: показан реальный статус интеграции.
          Runtime-параметры этого модуля доступны в «Настройки».
        </p>
      </div>
    </>
  );
}
