# Status — 22.2.9

[Back to README](../README.md)

PC-to-PS3 conversion, automatic texture budgeting, loading-picture preparation,
menu relocation and export reopening are available. Converted maps have reached
gameplay in console tests; this does not establish universal map compatibility.

The custom private-match launch script temporarily disables `useSvMapPreloading`
before direct map startup. It saves the previous value, queues restoration and
handles an interrupted override on the next private launch. BLES startup is
confirmed. The captured reference/BLES and BLUS UI exports passed the native
loader check. The tested BLUS-30072 console setup is also confirmed working.
Second-console joining and runtime restoration still require validation.
No EBOOT patch is needed.

Remaining work: PS3 hardware testing of compiled-RSX zones, second-console
joining/stock-map restoration, loading-screen title localization (the title
shown while the map is still loading lives in the UI/localized zones), and
regional UI profiles beyond the captured ones (the capture tool supports
adding them from an untouched ui_mp.ff).
