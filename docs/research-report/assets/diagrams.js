/* Mermaid initialization — palette: Business Blue (values must be concrete) */
(function () {
  if (typeof mermaid === 'undefined') return;
  mermaid.initialize({
    startOnLoad: true,
    theme: 'base',
    securityLevel: 'loose',
    flowchart: { htmlLabels: true, curve: 'basis', nodeSpacing: 48, rankSpacing: 48 },
    themeVariables: {
      fontFamily: "system-ui, 'PingFang SC', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif",
      fontSize: '14px',
      primaryColor: '#EEF4FB',
      primaryBorderColor: '#0969DA',
      primaryTextColor: '#1F2328',
      secondaryColor: '#F6F8FA',
      tertiaryColor: '#FFFFFF',
      lineColor: '#656D76',
      textColor: '#1F2328',
      edgeLabelBackground: '#FFFFFF',
      clusterBkg: '#F6F8FA',
      clusterBorder: '#D8DEE4',
      titleColor: '#1F2328'
    }
  });
})();
