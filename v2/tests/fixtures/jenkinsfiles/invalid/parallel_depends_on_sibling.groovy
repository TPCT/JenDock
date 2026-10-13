// expect: line 8: not built by an earlier dockerBuild
pipeline {
    agent any
    stages {
        stage('Both') {
            parallel {
                stage('Build') { steps { dockerBuild(image: 'app') } }
                stage('Ship') { steps { deploy service: 'app', image: 'app' } }
            }
        }
    }
}
