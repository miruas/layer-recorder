#!/usr/bin/env python3
"""Exercise native settings against an isolated temporary configuration."""
import argparse,json,os,sys,tempfile
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('--screenshots',type=Path);args=parser.parse_args()
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
with tempfile.TemporaryDirectory(prefix='layer-ui-qa-') as directory:
    root=Path(directory);os.environ['XDG_CONFIG_HOME']=str(root/'config');os.environ['XDG_CACHE_HOME']=str(root/'cache');os.environ['LAYER_CONFIG']=str(root/'settings.json')
    from PySide6.QtWidgets import QApplication,QDialog,QComboBox,QDialogButtonBox
    from PySide6.QtCore import QTimer
    from katman.common import atomic_json,settings
    from katman.configuration import profile
    import katman.theme
    katman.theme.THEME_ROOT=root/'no-external-themes'
    cfg=profile('auto');cfg['library']=str(root/'recordings');atomic_json(root/'settings.json',cfg)
    from katman.ui import Window
    app=QApplication([]);app.setStyle('Fusion');w=Window(test_mode=True);w.show();w.resize(1100,740);state={'ok':False}
    def edit():
        dialog=app.activeModalWidget();assert isinstance(dialog,QDialog)
        combos={c.accessibleName():c for c in dialog.findChildren(QComboBox)}
        vendor=combos['GPU profile'];vendor.setCurrentIndex(vendor.findData('amd'));combos['Capture codec'].setCurrentText('hevc')
        if args.screenshots:
            args.screenshots.mkdir(parents=True,exist_ok=True);dialog.grab().save(str(args.screenshots/'settings.png'));w.grab().save(str(args.screenshots/'library.png'))
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Save).click()
    def run():
        QTimer.singleShot(100,edit);w.show_settings();values=settings();assert values['gpu_vendor']=='amd' and values['capture_codec']=='hevc';state['ok']=True;w.close();app.quit()
    QTimer.singleShot(100,run);QTimer.singleShot(15000,lambda:app.exit(9));result=app.exec();assert result==0 and state['ok'];print('NATIVE_SETTINGS_GPU_PROFILE_SAVE_OK')
