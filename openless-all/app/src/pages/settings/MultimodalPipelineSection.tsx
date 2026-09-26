// Advanced → Experimental: master switch for the multimodal recognition pipeline (issue #902).
// When enabled, a "legacy mode / multimodal mode" switch appears at the top of Services → AI
// Providers: multimodal mode completes "prompt + audio → final text" in one step with a single
// model; credentials live in the separate omni namespace, fully isolated from the two legacy
// ASR/LLM configurations — coexisting but inactive.

import { useTranslation } from 'react-i18next';
import { useHotkeySettings } from '../../state/HotkeySettingsContext';
import { Card } from '../_atoms';
import { ExperimentalSectionTitle, SettingRow, Toggle } from './shared';

export function MultimodalPipelineSection() {
  const { t } = useTranslation();
  const { prefs, updatePrefs } = useHotkeySettings();

  if (!prefs) {
    return (
      <Card>
        <div style={{ fontSize: 12, color: 'var(--ol-ink-4)' }}>{t('common.loading')}</div>
      </Card>
    );
  }

  const onToggle = (multimodalPipelineEnabled: boolean) => {
    void updatePrefs((current) => ({ ...current, multimodalPipelineEnabled })).catch((error) => {
      console.error('[settings] failed to update multimodal pipeline flag', error);
    });
  };

  return (
    <Card>
      <ExperimentalSectionTitle
        badge={t('common.experimental')}
        hint={t('settings.advanced.multimodalPipelineTitleHint')}
      >
        {t('settings.advanced.multimodalPipelineTitle')}
      </ExperimentalSectionTitle>
      <SettingRow
        label={t('settings.advanced.multimodalPipelineLabel')}
        desc={t('settings.advanced.multimodalPipelineHint')}
      >
        <Toggle on={prefs.multimodalPipelineEnabled} onToggle={onToggle} />
      </SettingRow>
    </Card>
  );
}
