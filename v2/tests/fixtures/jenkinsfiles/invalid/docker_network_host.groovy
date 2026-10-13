// expect: line 3: only allows 'none'
pipeline {
    agent { docker { image 'node:22'; args '--network host' } }
    stages { stage('A') { steps { sh 'id' } } }
}
