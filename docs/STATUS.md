# Status — 22.1.21

[Back to README](../README.md)

PC-to-PS3 conversion, automatic texture budgeting, loading-picture preparation,
menu relocation and export reopening are available. Converted maps have reached
gameplay in console tests; this does not establish universal map compatibility.

The custom private-match launch script temporarily disables `useSvMapPreloading`
before direct map startup. It saves the previous value, queues restoration and
handles an interrupted override on the next private launch. BLES startup is
confirmed. The captured reference/BLES and BLUS UI exports passed the native
loader check. BLUS console execution, second-console joining and runtime
restoration still require validation. No EBOOT patch is needed.

Remaining work includes general PC-to-RSX shader compilation, complete RSX
preview execution, particle simulation, material/effect fidelity and broader
regional support. Separate loading-title/in-game localization issues remain.

The GitHub source preparation changes documentation and test-fixture defaults;
conversion, menu-launch behavior and C# application code remain unchanged.
