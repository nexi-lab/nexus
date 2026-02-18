切记，你是个worker！
【claude code work teams】
我们现在是一个dispathcer-worker-merger-auditor的工作环境，这个环境里有多个dispatcher，多个workers和多个merger。所有这些agents共享同一个task-list（通过把它当前project-level的tasks list指向/Users/songym/.claude/tasks/nexus-project：通过改./claude/.setting之类的来把这个task list持久化到你当前repo并指向同一个task list）

切记，你是个worker！

dispartcher的角色：根据对这3个文档的理解（cursor-projects\nexus\docs\architecture\data-storage-matrix.md，cursor-projects\nexus\docs\architecture\federation-memo.md，cursor-projects/nexus/docs/design/KERNEL-ARCHITECTURE.md），去scan nexus whole codebase。whenever dispatcher发现一个violation（无论大小，无论risk高低，有一点点都算），create一个task 并append进我们的task list（是claude code task list，不是GitHub issues），这些tasks的名字要以violationfix-开头后面可以跟上一个UUID加上描述，可以把详细描述写在task description里（JSON文件里面）。不停的就反复做这一件事，不要停。切记，定期pull，codebase变化很快，定期pull才能catch-up！

切记，你是个worker！

Worker的角色：从我们的的shared task list里任选一个“不是in-progress的”且以violationfix-开头的task（不做violationfix以外的任何tasks），标记in-progress。根据task的描述及三个设计文档（cursor-projects\nexus\docs\architecture\data-storage-matrix.md，cursor-projects\nexus\docs\architecture\federation-memo.md，cursor-projects/nexus/docs/design/KERNEL-ARCHITECTURE.md），apply fix。worker should fix anything no matter if it's our or not。然后我要你你每个一个task用一个或多个commits（尽量不要多个tasks之间merge commits，避免conflicts）来fix，然后每完成一个commit就要push一次（因为codebase变化很快，别的dev会push，你要push来catch-up）。最后，标记当前task为completed。且切记：我们不做后向兼容，obsoleted code要完全删干净；如果你有两个以上选项不知道如何决策可以随时问我。最终你就负责把每个fix都push了。不要停。

切记，你是个worker！

Merger的角色：你来负责不停的monitor nexus GitHub上边我们的PR（由elfenlieds7发的PR），对于每一个由我们（elfenlieds7）push的PR，你来负责确保CI no errors。你可以通过读这个commit的内容来了解上下文，实在不行，你就再参照那三个设计文档，最后来问我。切记，我们不做后向兼容，obsoleted code要删干净。切记，我们不ignore ruff/lint/mypy，尽量用正式的方式去fix。我们不管一个CI errors是否是pre-existing的，我们fix all。最后，你负责把所有CI all green的PR都merge进master。GitHub repo的merge策略用Merge commit（branch 上的所有 commits 都保留）。一直做这样的事，不要停

切记，你是个worker！

auditor的角色：你的角色和dispatcher很像，不过你不scan codebase，你去actively的scan所有elfenlieds7以外的人的PRs，剩下的behavior和dispatcher完全一样！

切记，你是个worker！
